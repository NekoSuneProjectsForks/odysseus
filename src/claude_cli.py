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

# asyncio's default StreamReader line-buffer limit is 64 KiB. Claude Code puts
# one whole tool call (e.g. a full file being written/edited, or large bash
# output) on a single stream-json line, which routinely exceeds that in
# coding-agent mode and raises LimitOverrunError — crashing the entire agent
# run instead of just this turn. Give the pipe a much larger ceiling.
_STDOUT_BUFFER_LIMIT = 64 * 1024 * 1024


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


def _format_tool_command(name: str, tool_input: Dict[str, Any]) -> str:
    """Best-effort one-line summary of a tool call's input for the UI's
    tool_start bubble — mirrors the display Odysseus's own bash/read/write/
    edit tools already produce, so the two look consistent in the timeline."""
    if not isinstance(tool_input, dict):
        return ""
    if name == "Bash":
        return str(tool_input.get("command") or "")
    if name in ("Read", "Write", "Edit"):
        return str(tool_input.get("file_path") or tool_input.get("path") or "")
    if name in ("Glob", "Grep"):
        return str(tool_input.get("pattern") or "")
    for v in tool_input.values():
        if isinstance(v, str) and v:
            return v
    return ""


def _stringify_tool_result_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def _translate_claude_cli_event(obj: Dict[str, Any], tool_names: Dict[str, str]) -> List[Dict[str, Any]]:
    """`tool_names` is a per-stream id->name map the caller keeps alive across
    calls, so a later tool_result (`user` message) can be paired back up with
    the tool_use (`assistant` message) that started it — Claude Code reports
    those as two separate whole messages, not one paired event."""
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
    elif t == "assistant":
        # Whole-message tool_use blocks — Claude Code's own tool loop, distinct
        # from Odysseus's tool schemas, so this is progress feedback only
        # (never dispatched through Odysseus's tool_execution).
        message = obj.get("message") or {}
        for block in message.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                name = block.get("name") or "tool"
                block_id = block.get("id")
                if block_id:
                    tool_names[block_id] = name
                events.append({
                    "type": "tool_start",
                    "tool": name,
                    "command": _format_tool_command(name, block.get("input") or {}),
                })
    elif t == "user":
        message = obj.get("message") or {}
        content = message.get("content")
        for block in (content if isinstance(content, list) else []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                name = tool_names.get(block.get("tool_use_id"), "tool")
                events.append({
                    "type": "tool_output",
                    "tool": name,
                    "output": _stringify_tool_result_content(block.get("content"))[:2000],
                })
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
        {"type": "tool_start", "tool": str, "command": str}
        {"type": "tool_output", "tool": str, "output": str}
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
            limit=_STDOUT_BUFFER_LIMIT,
        )
    except Exception as e:
        yield {"type": "error", "message": f"Failed to start Claude Code CLI: {e}"}
        return

    saw_recognized_event = False
    unrecognized_snippet = ""
    errored = False
    tool_names: Dict[str, str] = {}
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
                yield {"type": "error", "message": "Claude Code CLI output exceeded the buffer limit for a single event."}
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
                # CLI ever entered stream-json mode. Keep a snippet so a
                # failure isn't silently swallowed below.
                if len(unrecognized_snippet) < 500:
                    unrecognized_snippet += text + "\n"
                continue
            for event in _translate_claude_cli_event(obj, tool_names):
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
