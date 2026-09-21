"""Telegram-free tmux executor for remote worker sessions."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import tmux_input_transport
from .codex_startup import (
    CODEX_READY_SETTLE_SECONDS,
    CodexStartupError,
    drive_codex_startup,
)
from .transcript_parser import TranscriptParser
from .utils import ccbot_dir

logger = logging.getLogger(__name__)


async def dispatch_create_session(
    executor: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    """Adapt the extensible wire payload to the worker executor contract."""
    kwargs = {
        "path": str(payload.get("path", "")),
        "backend": str(payload.get("backend", "")),
        "name": str(payload.get("name", "")),
        "startup_id": str(payload.get("startup_id", "")),
    }
    kwargs.update(
        {
            key: str(payload[key])
            for key in ("resume_session_id", "source_backend")
            if payload.get(key)
        }
    )
    return await executor.create_session(**kwargs)


@dataclass
class _TmuxWorkerSession:
    session_id: str
    window_id: str
    backend: str
    transcript_path: Path | None = None
    transcript_offset: int = 0
    pending_tools: dict[str, Any] = field(default_factory=dict)
    recovered: bool = False
    provider_session_id: str = ""
    provider_transcript_path: str = ""
    binding_announced: bool = False


class TmuxWorkerExecutor:
    """Small Telegram-free tmux executor used by the node-agent process."""

    def __init__(
        self,
        *,
        workdir: str | Path,
        tmux_session: str = "ccbot-worker",
        claude_command: str = "claude",
        codex_command: str = "codex",
        claude_flags: str = "",
        codex_flags: str = "",
        ready_timeout: float = 120.0,
        context_limit_bytes: int = 0,
        reconcile_interval: float = 10.0,
        codex_poll_interval: float = 0.25,
        codex_ready_settle_time: float = CODEX_READY_SETTLE_SECONDS,
    ):
        self._workdir = Path(workdir).expanduser().resolve()
        self._tmux_session = tmux_session
        self._claude_command = claude_command
        self._codex_command = codex_command
        self._claude_flags = claude_flags
        self._codex_flags = codex_flags
        self._ready_timeout = ready_timeout
        self._context_limit_bytes = max(0, context_limit_bytes)
        self._reconcile_interval = max(0.0, reconcile_interval)
        self._codex_poll_interval = max(0.0, codex_poll_interval)
        self._codex_ready_settle_time = max(0.0, codex_ready_settle_time)
        self._last_reconcile_at = time.monotonic()
        self._sessions: dict[str, _TmuxWorkerSession] = {}
        self._startup_windows: dict[str, str] = {}
        self._startup_sessions: dict[str, str] = {}
        self._cancelled_startups: set[str] = set()

    def capacity_snapshot(self) -> dict[str, int]:
        return {
            "active_sessions": len(self._sessions),
            # Zero is the wire-level representation for no ccbot-imposed cap.
            "max_sessions": 0,
        }

    async def list_directories(self, *, path: str) -> dict[str, Any]:
        directory = self._resolve_directory(path)
        try:
            entries = await asyncio.to_thread(
                lambda: [
                    entry.name
                    for entry in directory.iterdir()
                    if entry.is_dir()
                    and (not entry.name.startswith(".") or self._show_hidden_dirs())
                ]
            )
        except (OSError, PermissionError) as exc:
            raise ValueError(f"cannot list directory: {directory}") from exc
        entries.sort(key=str.casefold)
        return {"ok": True, "path": str(directory), "directories": entries}

    async def create_directory(self, *, path: str, name: str) -> dict[str, Any]:
        if not self._valid_directory_name(name):
            raise ValueError("invalid directory name")
        parent = self._resolve_directory(path)
        target = (parent / name).resolve()
        if target.parent != parent:
            raise ValueError("directory must remain under the selected path")
        existed = target.exists()
        try:
            await asyncio.to_thread(target.mkdir)
        except FileExistsError:
            if not target.is_dir():
                raise ValueError("path exists and is not a directory") from None
        except OSError as exc:
            raise ValueError(f"cannot create directory: {target}") from exc
        result = await self.list_directories(path=str(target))
        result["existed"] = existed
        return result

    async def create_session(
        self,
        *,
        path: str,
        backend: str,
        name: str,
        startup_id: str = "",
        resume_session_id: str = "",
        source_backend: str = "",
    ) -> dict[str, Any]:
        directory = self._resolve_directory(path)
        if backend not in ("claude", "codex"):
            raise ValueError(f"unsupported backend: {backend}")
        command = self._agent_command(backend)
        if resume_session_id:
            source_backend = source_backend or backend
            await asyncio.to_thread(
                self._restore_transcript_path,
                resume_session_id,
                str(directory),
                source_backend,
            )
            if source_backend == backend:
                command = self._agent_command(
                    backend, resume_session_id=resume_session_id
                )
            else:
                from .session_import import build_import_context, import_prompt
                from .session_models import Session

                archived = Session(
                    id=resume_session_id,
                    name=name,
                    backend=source_backend,
                    workdir=str(directory),
                    claude_session_id=resume_session_id,
                )
                context_path = await asyncio.to_thread(
                    build_import_context, archived, backend
                )
                command = self._agent_command(
                    backend,
                    initial_prompt=import_prompt(context_path, source_backend),
                )
        result = await self._start_tmux_agent(
            workdir=directory,
            backend=backend,
            name=name,
            command=command,
            startup_id=startup_id,
        )
        if resume_session_id and source_backend == backend:
            result["provider_session_id"] = resume_session_id
        return result

    async def resolve_provider_session(
        self, *, path: str, backend: str, session_id: str
    ) -> dict[str, Any]:
        """Recover one legacy routing id only when transcript identity is exact."""
        directory = self._resolve_directory(path)
        live = await self._find_session(session_id)
        if live is not None:
            await self._bind_transcript(live)
            if live.provider_session_id:
                return {
                    "ok": True,
                    "provider_session_id": live.provider_session_id,
                    "transcript_path": live.provider_transcript_path,
                }
        if backend == "codex":
            from .codex_session_io import list_sessions_for_directory
        elif backend == "claude":
            from .session_claude_io import list_sessions_for_directory
        else:
            return {"ok": False, "error_code": "unsupported_backend"}
        candidates = await list_sessions_for_directory(str(directory))
        if len(candidates) != 1:
            return {
                "ok": False,
                "error_code": (
                    "ambiguous_transcript" if candidates else "missing_transcript"
                ),
            }
        candidate = candidates[0]
        return {
            "ok": True,
            "provider_session_id": candidate.session_id,
            "transcript_path": candidate.file_path,
        }

    async def start_context_session(
        self, *, context_path: str, backend: str, name: str
    ) -> dict[str, Any]:
        result = await self._start_tmux_agent(
            workdir=self._workdir,
            backend=backend,
            name=name,
            command=self._agent_command(backend, context_path),
        )
        result.update(
            {
                "context_path": context_path,
                "context_error": (
                    "The transferred context exceeds the worker provider limit"
                    if self._context_limit_bytes
                    and Path(context_path).stat().st_size > self._context_limit_bytes
                    else ""
                ),
            }
        )
        return result

    async def _start_tmux_agent(
        self,
        *,
        workdir: Path,
        backend: str,
        name: str,
        command: str,
        startup_id: str = "",
        session_id: str = "",
    ) -> dict[str, Any]:
        if backend not in ("claude", "codex"):
            raise ValueError(f"unsupported backend: {backend}")
        if not self._workdir.is_dir():
            raise ValueError(f"worker workdir does not exist: {self._workdir}")
        if startup_id and startup_id in self._cancelled_startups:
            self._cancelled_startups.discard(startup_id)
            raise RuntimeError("worker session startup was cancelled")
        await self._recover_sessions(reconcile=True)
        await self._ensure_tmux_session()
        window_name = self._safe_window_name(name)
        code, stdout, stderr = await self._run_tmux(
            "new-window",
            "-t",
            self._tmux_session,
            "-P",
            "-F",
            "#{window_id}",
            "-n",
            window_name,
            "-c",
            str(workdir),
        )
        if code != 0:
            raise RuntimeError(stderr.strip() or "tmux new-window failed")
        window_id = stdout.strip()
        if not window_id:
            raise RuntimeError("tmux did not return a window id")
        if startup_id:
            self._startup_windows[startup_id] = window_id
        session_id = session_id or str(uuid.uuid4())
        try:
            if startup_id and startup_id in self._cancelled_startups:
                raise RuntimeError("worker session startup was cancelled")
            for option, value in (
                ("@ccbot_session_id", session_id),
                ("@ccbot_backend", backend),
            ):
                code, _stdout, stderr = await self._run_tmux(
                    "set-option", "-w", "-t", window_id, option, value
                )
                if code != 0:
                    raise RuntimeError(stderr.strip() or "tmux session metadata failed")
            code, _stdout, stderr = await self._run_tmux(
                "send-keys", "-t", window_id, command, "C-m"
            )
            if code != 0:
                raise RuntimeError(stderr.strip() or "tmux send-keys failed")
            await self._wait_ready(window_id, backend, command=command)
            if startup_id and startup_id in self._cancelled_startups:
                raise RuntimeError("worker session startup was cancelled")
        except BaseException:
            self._sessions.pop(session_id, None)
            try:
                cleanup_code, _stdout, cleanup_stderr = await self._run_tmux(
                    "kill-window", "-t", window_id
                )
                if cleanup_code != 0:
                    logger.error(
                        "Failed to roll back worker window %s: %s",
                        window_id,
                        cleanup_stderr.strip() or "tmux kill-window failed",
                    )
            except BaseException as cleanup_error:
                logger.error(
                    "Failed to roll back worker window %s: %s",
                    window_id,
                    cleanup_error,
                )
            raise
        finally:
            if startup_id:
                self._startup_windows.pop(startup_id, None)
                self._cancelled_startups.discard(startup_id)
        self._sessions[session_id] = _TmuxWorkerSession(
            session_id=session_id,
            window_id=window_id,
            backend=backend,
        )
        if startup_id:
            self._startup_sessions[startup_id] = session_id
        return {
            "target_window_id": window_id,
            "target_workdir": str(workdir),
            "target_agent_session_id": session_id,
        }

    async def send_text(self, *, session_id: str, text: str) -> dict[str, Any]:
        session = await self._find_session(session_id)
        if session is None:
            return {"ok": False, "error": "worker session not found"}
        await self._bind_transcript(session)
        ok = await tmux_input_transport.send_literal_chunked(
            session.window_id, text, backend=session.backend
        )
        return {"ok": ok, "error": "" if ok else "tmux input failed"}

    async def send_key(self, *, session_id: str, key: str) -> dict[str, Any]:
        session = await self._find_session(session_id)
        if session is None or not key:
            return {"ok": False, "error": "worker session not found"}
        code, _stdout, stderr = await self._run_tmux(
            "send-keys", "-t", session.window_id, key
        )
        return {"ok": code == 0, "error": "" if code == 0 else stderr.strip()}

    async def capture_session(self, *, session_id: str) -> dict[str, Any]:
        session = await self._find_session(session_id)
        if session is None:
            return {"ok": False, "error": "worker session not found"}
        code, stdout, stderr = await self._run_tmux(
            "capture-pane", "-p", "-t", session.window_id
        )
        return {
            "ok": code == 0,
            "pane": stdout if code == 0 else "",
            "error": "" if code == 0 else stderr.strip(),
        }

    async def terminate_session(self, *, session_id: str) -> dict[str, Any]:
        session = await self._find_session(session_id)
        if session is None:
            return {"ok": True}
        code, _stdout, stderr = await self._run_tmux(
            "kill-window", "-t", session.window_id
        )
        if code == 0:
            self._sessions.pop(session_id, None)
            for startup_id, created_session_id in tuple(self._startup_sessions.items()):
                if created_session_id == session_id:
                    self._startup_sessions.pop(startup_id, None)
        return {"ok": code == 0, "error": "" if code == 0 else stderr.strip()}

    async def cancel_session_start(self, *, startup_id: str) -> dict[str, Any]:
        if not startup_id:
            raise ValueError("startup_id is required")
        session_id = self._startup_sessions.pop(startup_id, "")
        if session_id:
            return await self.terminate_session(session_id=session_id)
        self._cancelled_startups.add(startup_id)
        window_id = self._startup_windows.get(startup_id, "")
        if window_id:
            await self._run_tmux("kill-window", "-t", window_id)
        return {"ok": True}

    async def _find_session(self, session_id: str) -> _TmuxWorkerSession | None:
        session = self._sessions.get(session_id)
        if session is None:
            await self._recover_sessions()
            session = self._sessions.get(session_id)
        return session

    async def poll_events(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if (
            self._reconcile_interval == 0
            or now - self._last_reconcile_at >= self._reconcile_interval
        ):
            await self._recover_sessions(reconcile=True)
            self._last_reconcile_at = now
        events: list[dict[str, Any]] = []
        for session in tuple(self._sessions.values()):
            try:
                await self._bind_transcript(session)
                if getattr(session, "provider_session_id", "") and not getattr(
                    session, "binding_announced", False
                ):
                    events.append(
                        {
                            "event_type": "session_binding",
                            "session_id": session.session_id,
                            "provider_session_id": session.provider_session_id,
                            "transcript_path": session.provider_transcript_path,
                        }
                    )
                    session.binding_announced = True
                path = session.transcript_path
                if path is None:
                    continue
                chunk, end_offset = await asyncio.to_thread(
                    self._read_transcript_tail, path, session.transcript_offset
                )
                if not chunk or not chunk.endswith(b"\n"):
                    continue
                rows = []
                for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
                    row = TranscriptParser.parse_line(raw_line)
                    if row:
                        rows.append(row)
                parsed, remaining = TranscriptParser.parse_entries(
                    rows, pending_tools=session.pending_tools
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "worker event poll failed session=%s window=%s transcript=%s",
                    session.session_id,
                    session.window_id,
                    getattr(session, "transcript_path", None),
                )
                continue
            # Commit parser state only after the complete chunk was accepted.
            # A transient read/parser failure therefore retries the same bytes.
            session.transcript_offset = end_offset
            session.pending_tools = remaining
            for entry in parsed:
                if entry.role not in ("user", "assistant") or not entry.text:
                    continue
                events.append(
                    {
                        "event_type": "session_message",
                        "session_id": session.session_id,
                        "role": entry.role,
                        "text": entry.text,
                        "content_type": entry.content_type,
                        "tool_use_id": entry.tool_use_id,
                        "tool_name": entry.tool_name,
                        "stop_reason": entry.stop_reason,
                        "timestamp": entry.timestamp or "",
                        "is_error": entry.is_error,
                        "api_error": entry.api_error,
                    }
                )
        return events

    @staticmethod
    def _read_transcript_tail(path: Path, offset: int) -> tuple[bytes, int]:
        size = path.stat().st_size
        start = offset if size >= offset else 0
        if size == start:
            return b"", start
        with path.open("rb") as stream:
            stream.seek(start)
            chunk = stream.read()
        return chunk, start + len(chunk)

    async def _bind_transcript(self, session: _TmuxWorkerSession) -> None:
        if session.transcript_path is not None and getattr(
            session, "provider_session_id", ""
        ):
            return

        def lookup() -> tuple[str, Path] | None:
            path = ccbot_dir() / "session_map.json"
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                return None
            if not isinstance(data, dict):
                return None
            suffix = f":{session.window_id}"
            for key, value in data.items():
                if not str(key).endswith(suffix) or not isinstance(value, dict):
                    continue
                transcript = Path(str(value.get("transcript_path", "")))
                provider_session_id = str(value.get("session_id", ""))
                if provider_session_id and transcript.is_file():
                    return provider_session_id, transcript
            return None

        binding = await asyncio.to_thread(lookup)
        if binding is None:
            return
        provider_session_id, path = binding
        session.transcript_path = path
        session.provider_session_id = provider_session_id
        session.provider_transcript_path = str(path)
        try:
            # A newly-created Codex session can publish session_map only after
            # its first response is already in the rollout. Read that rollout
            # from byte zero. Recovered tmux sessions tail their existing file
            # to avoid replaying already-delivered history after agent restart.
            session.transcript_offset = (
                path.stat().st_size if getattr(session, "recovered", False) else 0
            )
        except OSError:
            session.transcript_offset = 0
        logger.info(
            "worker transcript bound session=%s window=%s offset=%d recovered=%s",
            session.session_id,
            session.window_id,
            session.transcript_offset,
            getattr(session, "recovered", False),
        )

    async def _recover_sessions(self, *, reconcile: bool = False) -> None:
        code, stdout, _stderr = await self._run_tmux(
            "list-windows",
            "-t",
            self._tmux_session,
            "-F",
            "#{window_id}\t#{@ccbot_session_id}\t#{@ccbot_backend}",
        )
        if code != 0:
            if reconcile:
                self._sessions.clear()
            return
        recovered: dict[str, _TmuxWorkerSession] = {}
        for line in stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            window_id, session_id, backend = parts
            if not window_id or not session_id or backend not in ("claude", "codex"):
                continue
            existing = self._sessions.get(session_id)
            recovered[session_id] = existing or _TmuxWorkerSession(
                session_id=session_id,
                window_id=window_id,
                backend=backend,
                recovered=True,
            )
        if reconcile:
            self._sessions = recovered
        else:
            self._sessions.update(recovered)

    async def _ensure_tmux_session(self) -> None:
        code, _stdout, _stderr = await self._run_tmux(
            "has-session", "-t", self._tmux_session
        )
        if code == 0:
            return
        code, _stdout, stderr = await self._run_tmux(
            "new-session",
            "-d",
            "-s",
            self._tmux_session,
            "-c",
            str(self._workdir),
        )
        if code != 0:
            raise RuntimeError(stderr.strip() or "tmux new-session failed")

    def _agent_command(
        self,
        backend: str,
        context_path: str = "",
        *,
        resume_session_id: str = "",
        initial_prompt: str = "",
    ) -> str:
        if backend == "codex":
            command = self._codex_command
            flags = self._codex_flags
        else:
            command = self._claude_command
            flags = self._claude_flags
        parts = [command]
        if flags:
            parts.append(flags)
        if resume_session_id:
            parts.extend(
                ["resume", shlex.quote(resume_session_id)]
                if backend == "codex"
                else ["--resume", shlex.quote(resume_session_id)]
            )
        if context_path:
            prompt = (
                "Read the complete transferred session context from "
                f"{context_path}. Continue the conversation from that context."
            )
            parts.append(shlex.quote(prompt))
        if initial_prompt:
            parts.append(shlex.quote(initial_prompt))
        return " ".join(parts)

    @staticmethod
    def _restore_transcript_path(session_id: str, workdir: str, backend: str) -> Path:
        if backend == "codex":
            from .codex_session_io import build_session_file_path
        elif backend == "claude":
            from .session_claude_io import build_session_file_path
        else:
            raise ValueError(f"unsupported source backend: {backend}")
        path = build_session_file_path(session_id, workdir)
        if path is None or not path.is_file():
            label = "Codex rollout" if backend == "codex" else "Claude transcript"
            raise ValueError(f"{label} not found on worker")
        return path

    @staticmethod
    def _valid_directory_name(name: str) -> bool:
        return bool(
            name
            and name not in (".", "..")
            and "/" not in name
            and "\\" not in name
            and "\x00" not in name
        )

    @staticmethod
    def _show_hidden_dirs() -> bool:
        return os.environ.get("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"

    @staticmethod
    def _resolve_directory(path: str) -> Path:
        directory = Path(path).expanduser().resolve() if path else Path.home().resolve()
        if not directory.exists() or not directory.is_dir():
            raise ValueError(f"directory does not exist: {directory}")
        return directory

    async def _wait_ready(
        self, window_id: str, backend: str, *, command: str = ""
    ) -> None:
        if backend == "codex":
            if not command:
                raise ValueError("Codex startup requires its relaunch command")

            async def capture() -> str:
                code, stdout, stderr = await self._run_tmux(
                    "capture-pane", "-p", "-t", window_id
                )
                if code != 0:
                    raise CodexStartupError(
                        stderr.strip() or "worker Codex pane disappeared"
                    )
                return stdout

            async def current_process() -> str:
                code, stdout, stderr = await self._run_tmux(
                    "display-message",
                    "-p",
                    "-t",
                    window_id,
                    "#{pane_current_command}",
                )
                if code != 0:
                    raise CodexStartupError(
                        stderr.strip() or "worker Codex pane disappeared"
                    )
                return stdout.strip()

            async def send_key(key: str) -> None:
                tmux_key = "C-m" if key == "ENTER" else "Down"
                code, _stdout, stderr = await self._run_tmux(
                    "send-keys", "-t", window_id, tmux_key
                )
                if code != 0:
                    raise CodexStartupError(stderr.strip() or "tmux send-keys failed")

            async def relaunch(exact_command: str) -> None:
                code, _stdout, stderr = await self._run_tmux(
                    "send-keys", "-t", window_id, exact_command, "C-m"
                )
                if code != 0:
                    raise CodexStartupError(stderr.strip() or "Codex relaunch failed")

            await drive_codex_startup(
                command=command,
                capture=capture,
                current_process=current_process,
                send_key=send_key,
                relaunch=relaunch,
                timeout=self._ready_timeout,
                poll_interval=self._codex_poll_interval,
                ready_settle_time=self._codex_ready_settle_time,
            )
            return

        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while asyncio.get_running_loop().time() < deadline:
            code, stdout, _stderr = await self._run_tmux(
                "capture-pane", "-p", "-t", window_id
            )
            pane = stdout if code == 0 else ""
            if pane:
                tail = pane.splitlines()[-8:]
                if any(line.lstrip().startswith((">", "❯")) for line in tail):
                    return
            await asyncio.sleep(1.0)
        raise TimeoutError("worker agent did not become ready")

    @staticmethod
    def _safe_window_name(name: str) -> str:
        value = " ".join(name.split()).strip() or "transferred"
        return value[:80]

    @staticmethod
    async def _run_tmux(*args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
