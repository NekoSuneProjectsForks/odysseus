"""Claude Code CLI provider — runs the official ``claude`` binary as a subprocess.

This is NOT a raw-API / OAuth-token-reuse integration. It shells out to the
real ``claude`` CLI, which authenticates itself via its own ``claude login``
(Anthropic account / Claude subscription OAuth) exactly as it would from an
interactive terminal — Odysseus never touches those credentials or the token
exchange. Users on API billing can equally point ``ANTHROPIC_API_KEY`` at the
CLI's environment instead of running ``claude login``; either way, the CLI's
own auth is used unmodified.

Odysseus treats this as just another ``ModelEndpoint`` by pointing its URL at
a sentinel that is never actually dialed (``CLAUDE_CLI_BASE_URL`` below) —
this lets it flow through the existing ``_detect_provider`` / per-provider
branch pattern in ``src/llm_core.py`` like any other backend.
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
CLAUDE_CLI_BASE_URL = "http://claude-code-cli.local"
CLAUDE_CLI_PROVIDER = "claude-cli"

# Aliases accepted by `claude --model`, not raw Anthropic model IDs — the CLI
# resolves these itself ("opusplan" is Claude Code specific: Opus for
# planning, Sonnet for execution).
CLAUDE_CLI_MODELS = ["sonnet", "opus", "haiku", "opusplan"]

DEFAULT_CLAUDE_CLI_TIMEOUT = int(os.getenv("CLAUDE_CLI_TIMEOUT", "600") or "600")


def is_claude_cli_base(url: str) -> bool:
    try:
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "claude-code-cli.local"


def find_claude_binary() -> Optional[str]:
    """Locate the `claude` CLI. CLAUDE_CLI_PATH overrides auto-detection for
    non-standard installs (e.g. a Docker image that installed it elsewhere)."""
    override = os.getenv("CLAUDE_CLI_PATH", "").strip()
    if override:
        return override if (shutil.which(override) or os.path.isfile(override)) else None
    return shutil.which("claude")


def is_claude_cli_available() -> bool:
    return find_claude_binary() is not None


def _last_user_text(messages: List[Dict]) -> str:
    """Claude Code CLI keeps its own conversation state via --resume, so it
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


def build_claude_cli_args(
    *,
    model: str,
    prompt: str,
    system_prompt: str = "",
    resume_session_id: Optional[str] = None,
    workspace: Optional[str] = None,
) -> List[str]:
    resolved_model = model if model in CLAUDE_CLI_MODELS else "sonnet"
    args = [
        "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model", resolved_model,
    ]
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if system_prompt:
        args += ["--append-system-prompt", system_prompt]
    if workspace:
        # Coding-agent mode: `workspace` is already a single Odysseus-confined
        # folder — let Claude Code use its own file/bash tools scoped to that
        # same cwd, auto-accepting edits since there is no terminal attached
        # to approve them from.
        args += ["--allowedTools", "Bash,Read,Edit,Write,Glob,Grep", "--permission-mode", "acceptEdits"]
    else:
        # Plain-chat mode: Odysseus's own agent loop (and its own tool
        # security/sandboxing) is what should execute tools, not a second,
        # unaudited tool surface inside the CLI subprocess. An empty allow
        # list makes any tool attempt auto-deny in this non-interactive mode.
        args += ["--allowedTools", ""]
    return args


def _translate_claude_cli_event(obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        sid = obj.get("session_id")
        if sid:
            events.append({"type": "session_id", "id": sid})
    elif t == "stream_event":
        inner = obj.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                events.append({"type": "delta", "text": delta["text"], "thinking": False})
            elif delta.get("type") == "thinking_delta" and delta.get("thinking"):
                events.append({"type": "delta", "text": delta["thinking"], "thinking": True})
    elif t == "result":
        sid = obj.get("session_id")
        if sid:
            events.append({"type": "session_id", "id": sid})
        usage = obj.get("usage") or {}
        if usage:
            events.append({
                "type": "usage",
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
            })
        if obj.get("is_error"):
            events.append({"type": "error", "message": obj.get("result") or "Claude Code CLI reported an error."})
    return events


async def stream_claude_cli(
    *,
    messages: List[Dict],
    model: str,
    workspace: Optional[str] = None,
    resume_session_id: Optional[str] = None,
    timeout: int = DEFAULT_CLAUDE_CLI_TIMEOUT,
) -> AsyncIterator[Dict[str, Any]]:
    """Run `claude -p ...` and yield normalized events:
        {"type": "delta", "text": str, "thinking": bool}
        {"type": "session_id", "id": str}
        {"type": "usage", "input_tokens": int, "output_tokens": int}
        {"type": "error", "message": str}
        {"type": "done"}
    """
    binary = find_claude_binary()
    if not binary:
        yield {
            "type": "error",
            "message": (
                "Claude Code CLI not found on this server. Install it "
                "(npm install -g @anthropic-ai/claude-code) and run "
                "`claude login` (or set ANTHROPIC_API_KEY for it) first."
            ),
        }
        return

    prompt = _last_user_text(messages)
    if not prompt:
        yield {"type": "error", "message": "No user message to send to Claude Code CLI."}
        return

    system_prompt = _system_prompt(messages)
    cwd = workspace or os.getcwd()
    args = build_claude_cli_args(
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
        )
    except Exception as e:
        yield {"type": "error", "message": f"Failed to start Claude Code CLI: {e}"}
        return

    saw_recognized_event = False
    unrecognized_snippet = ""
    errored = False
    try:
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
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
                # CLI ever entered stream-json mode. Keep a snippet so a
                # failure isn't silently swallowed below.
                if len(unrecognized_snippet) < 500:
                    unrecognized_snippet += text + "\n"
                continue
            for event in _translate_claude_cli_event(obj):
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
            yield {"type": "error", "message": "Claude Code CLI timed out."}
            return

        if proc.returncode not in (0, None) and not saw_recognized_event:
            stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
            detail = (stderr.strip() or unrecognized_snippet.strip())[:500]
            yield {"type": "error", "message": f"Claude Code CLI exited with code {proc.returncode}: {detail}"}
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
    """Return a `claude --resume` id, but only when it was captured under the
    SAME cwd — Claude Code's own session state is filesystem-scoped, so
    resuming from a different workspace produces a confused conversation."""
    if not odysseus_session_id:
        return None
    Session, SessionLocal = _session_local()
    db = SessionLocal()
    try:
        row = db.query(Session).filter(Session.id == odysseus_session_id).first()
        if not row or not row.claude_cli_session_id:
            return None
        expected_cwd = workspace or os.getcwd()
        if (row.claude_cli_cwd or "") != expected_cwd:
            return None
        return row.claude_cli_session_id
    except Exception as e:
        logger.warning("Failed to read claude_cli resume state: %s", e)
        return None
    finally:
        db.close()


def save_resume_session_id(odysseus_session_id: Optional[str], workspace: Optional[str], claude_session_id: str) -> None:
    if not odysseus_session_id or not claude_session_id:
        return
    Session, SessionLocal = _session_local()
    db = SessionLocal()
    try:
        row = db.query(Session).filter(Session.id == odysseus_session_id).first()
        if not row:
            return
        row.claude_cli_session_id = claude_session_id
        row.claude_cli_cwd = workspace or os.getcwd()
        db.commit()
    except Exception as e:
        logger.warning("Failed to persist claude_cli resume state: %s", e)
        db.rollback()
    finally:
        db.close()
