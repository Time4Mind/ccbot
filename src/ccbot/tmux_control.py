"""Persistent read-only tmux control-mode client."""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class TmuxControlClient:
    """Run repeated tmux reads without spawning one process per command."""

    def __init__(self, session_name: str) -> None:
        self.session_name = session_name
        self.proc: asyncio.subprocess.Process | None = None
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        proc = self.proc
        self.proc = None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.5)
        except TimeoutError:
            proc.kill()
            await proc.wait()

    async def start(self) -> bool:
        if self.proc is not None and self.proc.returncode is None:
            return True
        try:
            proc = await asyncio.create_subprocess_exec(
                "/usr/bin/script",
                "-q",
                "/dev/null",
                "tmux",
                "-C",
                "attach-session",
                "-f",
                "read-only,ignore-size,no-output",
                "-t",
                self.session_name,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            logger.debug("Tmux control client start failed: %s", exc)
            return False
        self.proc = proc
        if proc.stdout is None or proc.stdin is None:
            await self.close()
            return False
        try:
            while True:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                if not raw:
                    raise RuntimeError("tmux control client exited during startup")
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line.startswith("%session-changed "):
                    return True
                if line.startswith("%exit"):
                    raise RuntimeError(line)
        except Exception as exc:
            logger.debug("Tmux control client handshake failed: %s", exc)
            await self.close()
            return False

    async def request(self, command: str) -> str | None:
        """Run one read-only command through the persistent client."""
        async with self.lock:
            if not await self.start():
                return None
            proc = self.proc
            assert (
                proc is not None and proc.stdin is not None and proc.stdout is not None
            )
            try:
                proc.stdin.write((command + "\n").encode())
                await proc.stdin.drain()
                in_block = False
                lines: list[str] = []
                while True:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=2.0)
                    if not raw:
                        raise RuntimeError("tmux control client closed")
                    line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                    if line.startswith("%begin "):
                        in_block = True
                        lines = []
                    elif in_block and line.startswith("%end "):
                        return "\n".join(lines) + ("\n" if lines else "")
                    elif in_block and line.startswith("%error "):
                        raise RuntimeError("tmux control command failed")
                    elif in_block:
                        lines.append(line)
            except Exception as exc:
                logger.debug("Tmux control request failed: %s", exc)
                await self.close()
                return None
