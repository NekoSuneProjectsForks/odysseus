"""OpenAI Codex CLI provider — runs the official ``codex`` binary as a subprocess.

Same pattern and same ethical boundary as ``src/claude_cli.py``: this shells
out to the real ``codex`` CLI, which authenticates itself via its own
``codex login`` (ChatGPT/Codex subscription OAuth, device-code flow, or an
API key) exactly as it would from an interactive terminal — Odysseus never
touches those credentials or the token exchange.

Odysseus treats this as just another ``ModelEndpoint`` by pointing its URL at
a sentinel that is never actually dialed (``CODEX_CLI_BASE_URL`` below), the
same way ``claude-code-cli.local`` works for the Claude CLI provider.

Event schema reference (``codex exec --json``, newline-delimited, discriminated
by a top-level ``type``): ``thread.started`` (carries ``thread_id`` — the
resume id), ``item.completed`` (carries a completed ``item``, e.g.
``agent_message``/``reasoning`` with a ``text`` field — Codex reports whole
items rather than per-token deltas), ``turn.completed`` (carries ``usage``),
and ``turn.failed`` / ``error`` for failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Never actually dialed over the network — a hostname-matching sentinel so
# this provider fits the existing url -> provider detection pattern.
CODEX_CLI_BASE_URL = "http://codex-cli.local"
CODEX_CLI_PROVIDER = "codex-cli"

# Best-effort defaults; admins can add more via the endpoint's pinned_models
# escape hatch since Codex's model lineup isn't independently discoverable
# from this CLI the way a live /v1/models endpoint would be.
CODEX_CLI_MODELS = ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"]

DEFAULT_CODEX_CLI_TIMEOUT = int(os.getenv("CODEX_CLI_TIMEOUT", "600") or "600")

# asyncio's default StreamReader line-buffer limit is 64 KiB. Codex puts one
# whole completed item (e.g. a full file being written/edited, or large bash
# output) on a single --json line, which routinely exceeds that in
# coding-agent mode and raises LimitOverrunError — crashing the entire agent
# run instead of just this turn. Give the pipe a much larger ceiling.
_STDOUT_BUFFER_LIMIT = 64 * 1024 * 1024


def is_codex_cli_base(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "codex-cli.local"


def find_codex_binary() -> Optional[str]:
    """Locate the `codex` CLI. CODEX_CLI_PATH overrides auto-detection for
    non-standard installs (e.g. a Docker image that installed it elsewhere)."""
    override = os.getenv("CODEX_CLI_PATH", "").strip()
    if override:
        return override if (shutil.which(override) or os.path.isfile(override)) else None
    return shutil.which("codex")


def is_codex_cli_available() -> bool:
    return find_codex_binary() is not None


def _last_user_text(messages: List[Dict]) -> str:
    """Codex CLI keeps its own conversation state via `exec resume`, so it
    only ever needs the newest user turn — not Odysseus's full history."""
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p)
    return ""


def _system_prompt(messages: List[Dict]) -> str:
    parts = [
        m.get("content") or "" for m in messages
        if m.get("role") == "system" and isinstance(m.get("content"), str)
    ]
    return "\n\n".join(p for p in parts if p)


def build_codex_cli_args(
    *,
    model: str,
    prompt: str,
    system_prompt: str = "",
    resume_session_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> List[str]:
    # Codex has no independently documented model-alias list the way Claude
    # Code does, so an unrecognized value is passed through as-is (it may
    # still be a valid Codex model id set via pinned_models) rather than
    # silently substituted.
    full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
    args = ["exec"]
    if resume_session_id:
        args += ["resume", resume_session_id]
    args += [full_prompt, "--json", "--model", model, "--ask-for-approval", "never"]
    if workspace:
        # Coding-agent mode: `workspace` is already a single Odysseus-confined
        # folder — let Codex read/write/execute within that same cwd.
        args += ["--sandbox", "workspace-write"]
    else:
        # Plain-chat mode: Odysseus's own agent loop (and its own tool
        # security/sandboxing) is what should execute tools, not a second,
        # unaudited tool surface inside the CLI subprocess.
        args += ["--sandbox", "read-only"]
    return args


def _translate_codex_cli_event(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    t = obj.get("type")
    if t == "thread.started":
        tid = obj.get("thread_id")
        if tid:
            events.append({"type": "session_id", "id": tid})
    elif t == "item.completed":
        item = obj.get("item") or {}
        item_type = item.get("type")
        text = item.get("text")
        if item_type == "agent_message" and text:
            events.append({"type": "delta", "text": text, "thinking": False})
        elif item_type == "reasoning" and text:
            events.append({"type": "delta", "text": text, "thinking": True})
    elif t == "turn.completed":
        usage = obj.get("usage") or {}
        if usage:
            events.append({
                "type": "usage",
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            })
    elif t in ("turn.failed", "error"):
        err = obj.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
        else:
            msg = obj.get("message") or (str(err) if err else None)
        events.append({"type": "error", "message": msg or "Codex CLI reported an error."})
    return events


async def stream_codex_cli(
    *,
    messages: List[Dict],
    model: str,
    workspace: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    timeout: int = DEFAULT_CODEX_CLI_TIMEOUT,
) -> AsyncIterator[Dict[str, Any]]:
    """Run `codex exec ... --json` and yield normalized events:
        {"type": "delta", "text": str, "thinking": bool}
        {"type": "session_id", "id": str}
        {"type": "usage", "input_tokens": int, "output_tokens": int}
        {"type": "error", "message": str}
        {"type": "done"}
    """
    binary = find_codex_binary()
    if not binary:
        yield {
            "type": "error",
            "message": (
                "Codex CLI not found on this server. Install it "
                "(npm install -g @openai/codex) and run `codex login` "
                "(or set OPENAI_API_KEY for it) first."
            ),
        }
        return

    prompt = _last_user_text(messages)
    if not prompt:
        yield {"type": "error", "message": "No user message to send to Codex CLI."}
        return

    system_prompt = _system_prompt(messages)
    cwd = workspace or os.getcwd()
    args = build_codex_cli_args(
        model=model,
        prompt=prompt,
        system_prompt=system_prompt,
        resume_session_id=resume_session_id,
        workspace=workspace,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            binary, *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STDOUT_BUFFER_LIMIT,
        )
    except Exception as e:
        yield {"type": "error", "message": f"Failed to start Codex CLI: {e}"}
        return

    saw_recognized_event = False
    unrecognized_snippet = ""
    errored = False
    try:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                # A single line still exceeded _STDOUT_BUFFER_LIMIT
                # (LimitOverrunError, a ValueError subclass) — the stream is
                # unrecoverable at that point, so end the turn gracefully
                # instead of crashing the whole agent run.
                yield {"type": "error", "message": "Codex CLI output exceeded the buffer limit for a single event."}
                return
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                # Not a JSON event line — most likely a plain-text error
                # (not logged in, invalid model, etc.) printed before the
                # CLI ever entered --json mode. Keep a snippet so a failure
                # isn't silently swallowed below.
                if len(unrecognized_snippet) < 500:
                    unrecognized_snippet += text + "\n"
                continue
            for event in _translate_codex_cli_event(obj):
                saw_recognized_event = True
                if event.get("type") == "error":
                    errored = True
                yield event
            if errored:
                return

        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            yield {"type": "error", "message": "Codex CLI timed out."}
            return

        if proc.returncode not in (0, None) and not saw_recognized_event:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
            detail = (stderr.strip() or unrecognized_snippet.strip())[:500]
            yield {"type": "error", "message": f"Codex CLI exited with code {proc.returncode}: {detail}"}
            return
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    yield {"type": "done"}


def _session_local():
    from core.database import Session, SessionLocal
    return Session, SessionLocal


def get_resume_session_id(odysseus_session_id: Optional[str], workspace: Optional[str]) -> Optional[str]:
    """Return a `codex exec resume` id, but only when it was captured under
    the SAME cwd — Codex's own session state is filesystem-scoped, so
    resuming from a different workspace produces a confused conversation."""
    if not odysseus_session_id:
        return None
    Session, SessionLocal = _session_local()
    db = SessionLocal()
    try:
        row = db.query(Session).filter(Session.id == odysseus_session_id).first()
        if not row or not row.codex_cli_session_id:
            return None
        expected_cwd = workspace or os.getcwd()
        if (row.codex_cli_cwd or "") != expected_cwd:
            return None
        return row.codex_cli_session_id
    except Exception as e:
        logger.warning("Failed to read codex_cli resume state: %s", e)
        return None
    finally:
        db.close()


def save_resume_session_id(odysseus_session_id: Optional[str], workspace: Optional[str], codex_session_id: str) -> None:
    if not odysseus_session_id or not codex_session_id:
        return
    Session, SessionLocal = _session_local()
    db = SessionLocal()
    try:
        row = db.query(Session).filter(Session.id == odysseus_session_id).first()
        if not row:
            return
        row.codex_cli_session_id = codex_session_id
        row.codex_cli_cwd = workspace or os.getcwd()
        db.commit()
    except Exception as e:
        logger.warning("Failed to persist codex_cli resume state: %s", e)
        db.rollback()
    finally:
        db.close()
