"""
bash_sandbox.py

Opt-in Docker container-per-workspace sandboxing for the agent's `bash` tool
(todo.md §4 "Sandboxing question"). When enabled (Settings > Agent > "Sandbox
workspace commands"), bash commands run against an active workspace execute
inside a long-lived Docker container bind-mounting that workspace, instead of
directly on the host — confining `bash` the same way the workspace already
confines the file tools (see src/tool_execution.py).

Off by default: this is additive, not a replacement for the existing
unsandboxed path, and does nothing unless both the setting is on AND Docker
is reachable. One container per workspace path, reused across calls (kept
alive with `sleep infinity`) so installed packages / build artifacts persist
across turns, the same way a normal working directory would.
"""

import asyncio
import hashlib
import logging
import os
from typing import Tuple

logger = logging.getLogger(__name__)

DEFAULT_SANDBOX_IMAGE = "mcr.microsoft.com/devcontainers/base:ubuntu"
_CONTAINER_PREFIX = "odysseus-ws-"
_DOCKER_CMD_TIMEOUT = 15  # seconds, for docker CLI control calls (not the exec'd command itself)


class SandboxError(Exception):
    """Raised for user-facing errors (Docker unavailable, container setup failed)."""


async def _run(*args: str, timeout: float = _DOCKER_CMD_TIMEOUT) -> Tuple[str, str, int]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return "", "docker: command not found", 127
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "timed out", 124
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def is_docker_available() -> bool:
    _, _, rc = await _run("docker", "version", "--format", "{{.Server.Version}}", timeout=8)
    return rc == 0


def _container_name(workspace_path: str) -> str:
    """Deterministic, filesystem-independent name so the same workspace always
    maps to the same container (survives Odysseus restarts)."""
    digest = hashlib.sha256(os.path.realpath(workspace_path).encode("utf-8")).hexdigest()[:16]
    return f"{_CONTAINER_PREFIX}{digest}"


def _sandbox_image() -> str:
    from src.settings import get_setting
    return (get_setting("bash_sandbox_image", "") or DEFAULT_SANDBOX_IMAGE).strip() or DEFAULT_SANDBOX_IMAGE


async def _container_state(name: str) -> str:
    """Returns 'running', 'stopped', or 'missing'."""
    out, _, rc = await _run("docker", "inspect", "-f", "{{.State.Running}}", name)
    if rc != 0:
        return "missing"
    return "running" if out.strip() == "true" else "stopped"


async def ensure_container(workspace_path: str) -> str:
    """Get-or-create the sandbox container for this workspace. Returns the
    container name. Raises SandboxError on failure."""
    name = _container_name(workspace_path)
    state = await _container_state(name)
    if state == "running":
        return name
    if state == "stopped":
        _, err, rc = await _run("docker", "start", name, timeout=30)
        if rc != 0:
            raise SandboxError(f"Could not restart sandbox container: {err.strip() or 'unknown error'}")
        return name

    image = _sandbox_image()
    resolved = os.path.realpath(workspace_path)
    _, err, rc = await _run(
        "docker", "run", "-d", "--name", name,
        "-v", f"{resolved}:/workspace",
        "-w", "/workspace",
        image, "sleep", "infinity",
        timeout=120,  # first run may need to pull the image
    )
    if rc != 0:
        raise SandboxError(f"Could not start sandbox container ({image}): {err.strip() or 'unknown error'}")
    return name


async def exec_in_container(
    container_name: str, command: str, *, timeout: float,
) -> Tuple[str, str, int]:
    """Run `command` inside the container's default shell. Each call is a
    fresh shell invocation (no persistent state between calls beyond the
    container's own filesystem) — the same semantics as the existing
    non-tmux bash path, just executed inside Docker instead of on the host."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", container_name, "bash", "-lc", command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise SandboxError("docker CLI not found on this host")
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "", "", 124
    return (
        out_b.decode("utf-8", errors="replace"),
        err_b.decode("utf-8", errors="replace"),
        proc.returncode or 0,
    )


async def stop_sandbox(workspace_path: str) -> None:
    """Stop (but don't remove) the container for a workspace — frees resources
    while keeping any installed packages/artifacts for next time."""
    name = _container_name(workspace_path)
    await _run("docker", "stop", "-t", "5", name, timeout=15)


async def remove_sandbox(workspace_path: str) -> None:
    """Stop and permanently remove the container for a workspace."""
    name = _container_name(workspace_path)
    await _run("docker", "rm", "-f", name, timeout=15)
