"""Small, explicit installer for optional Claude/Codex CLI backends."""

from __future__ import annotations

import asyncio
import shlex
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from .config import config

Progress = Callable[[str], Awaitable[None]]

_PACKAGES = {
    "claude": "@anthropic-ai/claude-code",
    "codex": "@openai/codex",
}


def _command(backend: str) -> str:
    configured = config.claude_command if backend == "claude" else config.codex_command
    parts = shlex.split(configured)
    return parts[0] if parts else backend


def is_available(backend: str) -> bool:
    executable = _command(backend)
    path = Path(executable).expanduser()
    return (
        path.is_file() if path.is_absolute() else shutil.which(executable) is not None
    )


async def install(backend: str, progress: Progress) -> bool:
    """Install a missing official CLI package with npm."""
    if backend not in _PACKAGES:
        raise ValueError(f"Unknown backend: {backend}")
    if is_available(backend):
        return True
    npm = shutil.which("npm")
    if npm is None:
        await progress("❌ npm is unavailable; install Node.js/npm first.")
        return False
    package = _PACKAGES[backend]
    await progress(f"⏳ Installing {backend}: `{package}`")
    process = await asyncio.create_subprocess_exec(
        npm,
        "install",
        "-g",
        package,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    if process.returncode != 0:
        tail = output.decode(errors="replace").strip()[-800:]
        await progress(f"❌ {backend} install failed:\n```text\n{tail}\n```")
        return False
    if not is_available(backend):
        await progress(f"❌ {backend} was installed but its command is not on PATH.")
        return False
    await progress(f"✅ {backend.capitalize()} installed.")
    return True
