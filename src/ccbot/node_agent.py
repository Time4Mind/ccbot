"""Relay worker agent without Telegram or leader-state dependencies."""

from __future__ import annotations

import asyncio
import argparse
import base64
import hashlib
import json
import logging
import os
import secrets
import shlex
import ssl as ssl_module
import platform
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, cast
from urllib.parse import urlparse

from . import tmux_input_transport
from .node_transport import (
    NodeEnvelope,
    NodeTransport,
    RequestReceiptLedger,
    connect_relay,
)
from .node_pairing import PairingInvitation
from .terminal_parser import parse_status_line
from .transcript_parser import TranscriptParser
from .utils import ccbot_dir

logger = logging.getLogger(__name__)
HEALTH_INTERVAL_SECONDS = 15.0
_CONTROL_OPERATIONS = {"send_key", "capture_session", "terminate_session", "health"}
_COMMAND_QUEUE_SIZE = 256


class NodeCredentialStore:
    """Private worker credential used only to reconnect after process restart."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def load(self, *, node_id: str, leader_id: str, relay_url: str) -> str:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return ""
        if not isinstance(data, dict):
            return ""
        if (
            data.get("node_id") != node_id
            or data.get("leader_id") != leader_id
            or data.get("relay_url") != relay_url
        ):
            return ""
        return str(data.get("secret", ""))

    def save(
        self, *, node_id: str, leader_id: str, relay_url: str, secret: str
    ) -> None:
        if not secret:
            raise ValueError("reconnect secret is required")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "node_id": node_id,
                    "leader_id": leader_id,
                    "relay_url": relay_url,
                    "secret": secret,
                }
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)
        os.chmod(self.path, 0o600)


class WorkerSessionExecutor(Protocol):
    async def list_directories(self, *, path: str) -> dict[str, Any]: ...

    async def create_directory(self, *, path: str, name: str) -> dict[str, Any]: ...

    async def create_session(
        self, *, path: str, backend: str, name: str
    ) -> dict[str, Any]: ...

    async def start_context_session(
        self, *, context_path: str, backend: str, name: str
    ) -> dict[str, Any]: ...

    async def send_text(self, *, session_id: str, text: str) -> dict[str, Any]: ...

    async def send_key(self, *, session_id: str, key: str) -> dict[str, Any]: ...

    async def capture_session(self, *, session_id: str) -> dict[str, Any]: ...

    async def terminate_session(self, *, session_id: str) -> dict[str, Any]: ...


@dataclass
class _PendingContext:
    transfer_id: str
    path: Path
    expected_bytes: int
    expected_sha256: str
    backend: str
    name: str
    next_chunk: int = 0
    received_bytes: int = 0


class NodeAgent:
    """Handle typed leader commands on a worker connection."""

    def __init__(
        self,
        transport: NodeTransport,
        executor: WorkerSessionExecutor,
        *,
        context_dir: str | Path,
        node_id: str = "",
        display_name: str = "",
        backends: tuple[str, ...] = (),
        receipt_ledger: RequestReceiptLedger | None = None,
        max_context_bytes: int = 50 * 1024 * 1024,
    ):
        self._transport = transport
        self._executor = executor
        self._context_dir = Path(context_dir).expanduser()
        self._context_dir.mkdir(parents=True, exist_ok=True)
        self._node_id = node_id
        self._display_name = display_name or node_id
        self._backends = tuple(backends)
        self._ledger = receipt_ledger or RequestReceiptLedger()
        self._max_context_bytes = max_context_bytes
        self._pending_contexts: dict[str, _PendingContext] = {}

    def attach_transport(self, transport: NodeTransport) -> None:
        """Attach a reconnected relay while retaining receipts and context."""
        self._transport = transport

    async def run(self) -> None:
        await self._send_health()
        regular_commands: asyncio.Queue[NodeEnvelope] = asyncio.Queue(
            maxsize=_COMMAND_QUEUE_SIZE
        )
        control_commands: asyncio.Queue[NodeEnvelope] = asyncio.Queue(
            maxsize=_COMMAND_QUEUE_SIZE
        )
        heartbeat = asyncio.create_task(self._health_loop(), name="node-health")
        events = asyncio.create_task(self._event_loop(), name="node-session-events")
        regular_worker = asyncio.create_task(
            self._command_worker(regular_commands), name="node-regular-commands"
        )
        control_worker = asyncio.create_task(
            self._command_worker(control_commands), name="node-control-commands"
        )
        background = (heartbeat, events, regular_worker, control_worker)
        try:
            while True:
                message = await self._transport.receive()
                if message.kind != "command":
                    continue
                operation = str((message.payload or {}).get("operation", ""))
                queue = (
                    control_commands
                    if operation in _CONTROL_OPERATIONS
                    else regular_commands
                )
                await queue.put(message)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)

    async def _command_worker(self, queue: asyncio.Queue[NodeEnvelope]) -> None:
        while True:
            message = await queue.get()
            try:
                await self._handle_command(message)
            finally:
                queue.task_done()

    async def _send_health(self) -> None:
        capacity_snapshot = getattr(self._executor, "capacity_snapshot", None)
        capacity = capacity_snapshot() if callable(capacity_snapshot) else {}
        await self._transport.send(
            NodeEnvelope(
                kind="health",
                payload={
                    "node_id": self._node_id,
                    "display_name": self._display_name,
                    "state": "ready" if self._backends else "online",
                    "platform": platform.system(),
                    "arch": platform.machine(),
                    "backends": list(self._backends),
                    "capabilities": {
                        "directory_browser": True,
                        "create_session": True,
                        "context_transfer": True,
                        "send_text": True,
                    },
                    "capacity": capacity,
                },
            )
        )

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            try:
                await self._send_health()
            except (ConnectionError, asyncio.CancelledError):
                return

    async def _event_loop(self) -> None:
        poll_events = getattr(self._executor, "poll_events", None)
        if not callable(poll_events):
            return
        typed_poll = cast(Callable[[], Awaitable[list[dict[str, Any]]]], poll_events)
        while True:
            await asyncio.sleep(0.5)
            for payload in await typed_poll():
                await self._transport.send(
                    NodeEnvelope(
                        kind="event",
                        payload={"node_id": self._node_id, **payload},
                    )
                )

    async def _handle_command(self, message: NodeEnvelope) -> None:
        if not message.request_id:
            await self._send_error(message, "request_id is required")
            return
        existing = self._ledger.get(message.request_id)
        receipt = existing or self._ledger.accept(message.request_id)
        await self._transport.send(
            NodeEnvelope(
                kind="ack",
                request_id=message.request_id,
                payload={"accepted": existing is None},
            )
        )
        if receipt.result is not None:
            await self._transport.send(
                NodeEnvelope(
                    kind="result",
                    request_id=message.request_id,
                    payload=receipt.result,
                )
            )
            return
        if existing is not None:
            return
        try:
            result = await self._dispatch(message.payload or {})
        except Exception as exc:
            logger.exception(
                "node command failed operation=%s",
                (message.payload or {}).get("operation"),
            )
            result = {"ok": False, "error": str(exc)}
        self._ledger.complete(message.request_id, result)
        await self._transport.send(
            NodeEnvelope(kind="result", request_id=message.request_id, payload=result)
        )

    async def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation", ""))
        if operation == "transfer_context_begin":
            return self._begin_context(payload)
        if operation == "transfer_context_chunk":
            return self._append_context(payload)
        if operation == "transfer_context_finish":
            return await self._finish_context(payload)
        if operation == "list_directories":
            return await self._executor.list_directories(
                path=str(payload.get("path", ""))
            )
        if operation == "create_directory":
            return await self._executor.create_directory(
                path=str(payload.get("path", "")),
                name=str(payload.get("name", "")),
            )
        if operation == "create_session":
            return await self._executor.create_session(
                path=str(payload.get("path", "")),
                backend=str(payload.get("backend", "")),
                name=str(payload.get("name", "")),
            )
        if operation == "send_text":
            session_id = str(payload.get("session_id", ""))
            text = str(payload.get("text", ""))
            if not session_id or not text:
                raise ValueError("session_id and text are required")
            return await self._executor.send_text(session_id=session_id, text=text)
        if operation == "send_key":
            return await self._executor.send_key(
                session_id=str(payload.get("session_id", "")),
                key=str(payload.get("key", "")),
            )
        if operation == "capture_session":
            return await self._executor.capture_session(
                session_id=str(payload.get("session_id", ""))
            )
        if operation == "terminate_session":
            return await self._executor.terminate_session(
                session_id=str(payload.get("session_id", ""))
            )
        if operation == "health":
            return {"ok": True}
        raise ValueError(f"unsupported node operation: {operation}")

    def _begin_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        expected_bytes = int(payload.get("total_bytes", -1))
        expected_sha256 = str(payload.get("sha256", ""))
        if expected_bytes < 0 or expected_bytes > self._max_context_bytes:
            raise ValueError("context exceeds worker transfer limit")
        if len(expected_sha256) != 64:
            raise ValueError("context sha256 is required")
        transfer_id = secrets.token_urlsafe(12)
        path = self._context_dir / f"{transfer_id}.md"
        path.write_bytes(b"")
        self._pending_contexts[transfer_id] = _PendingContext(
            transfer_id=transfer_id,
            path=path,
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
            backend=str(payload.get("target_backend", "")),
            name=str(payload.get("source_name", "session")),
        )
        return {"ok": True, "transfer_id": transfer_id}

    def _append_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        transfer_id = str(payload.get("transfer_id", ""))
        pending = self._pending_contexts.get(transfer_id)
        if pending is None:
            raise ValueError("unknown context transfer")
        index = int(payload.get("chunk_index", -1))
        if index != pending.next_chunk:
            raise ValueError("context chunk sequence mismatch")
        try:
            chunk = base64.b64decode(str(payload.get("data", "")), validate=True)
        except Exception as exc:
            raise ValueError("invalid context chunk") from exc
        if pending.received_bytes + len(chunk) > pending.expected_bytes:
            raise ValueError("context byte count exceeds declared size")
        with pending.path.open("ab") as stream:
            stream.write(chunk)
        pending.received_bytes += len(chunk)
        pending.next_chunk += 1
        return {"ok": True, "received_bytes": pending.received_bytes}

    async def _finish_context(self, payload: dict[str, Any]) -> dict[str, Any]:
        transfer_id = str(payload.get("transfer_id", ""))
        pending = self._pending_contexts.pop(transfer_id, None)
        if pending is None:
            raise ValueError("unknown context transfer")
        content = await asyncio.to_thread(pending.path.read_bytes)
        if len(content) != pending.expected_bytes:
            raise ValueError("context byte count mismatch")
        if hashlib.sha256(content).hexdigest() != pending.expected_sha256:
            raise ValueError("context sha256 mismatch")
        result = await self._executor.start_context_session(
            context_path=str(pending.path),
            backend=pending.backend,
            name=pending.name,
        )
        result.setdefault("ok", True)
        result.setdefault("context_path", str(pending.path))
        return result

    async def _send_error(self, message: NodeEnvelope, error: str) -> None:
        await self._transport.send(
            NodeEnvelope(
                kind="error",
                request_id=message.request_id,
                payload={"ok": False, "error": error},
            )
        )

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        node_id: str,
        secret: str,
        leader_id: str,
        executor: WorkerSessionExecutor,
        context_dir: str | Path,
        display_name: str = "",
        backends: tuple[str, ...] = (),
        ssl: Any = None,
        pairing_nonce: str = "",
        pairing_expires: float = 0.0,
    ) -> "NodeAgent":
        transport = await connect_relay(
            host,
            port,
            node_id=node_id,
            role="worker",
            secret=secret,
            leader_id=leader_id,
            ssl=ssl,
            pairing_nonce=pairing_nonce,
            pairing_expires=pairing_expires,
        )
        return cls(
            transport,
            executor,
            context_dir=context_dir,
            node_id=node_id,
            display_name=display_name,
            backends=backends,
        )


@dataclass
class _TmuxWorkerSession:
    session_id: str
    window_id: str
    backend: str
    transcript_path: Path | None = None
    transcript_offset: int = 0
    pending_tools: dict[str, Any] = field(default_factory=dict)


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
        max_sessions: int = 8,
        reconcile_interval: float = 10.0,
    ):
        self._workdir = Path(workdir).expanduser().resolve()
        self._tmux_session = tmux_session
        self._claude_command = claude_command
        self._codex_command = codex_command
        self._claude_flags = claude_flags
        self._codex_flags = codex_flags
        self._ready_timeout = ready_timeout
        self._context_limit_bytes = max(0, context_limit_bytes)
        self._max_sessions = max(0, max_sessions)
        self._reconcile_interval = max(0.0, reconcile_interval)
        self._last_reconcile_at = time.monotonic()
        self._sessions: dict[str, _TmuxWorkerSession] = {}

    def capacity_snapshot(self) -> dict[str, int]:
        return {
            "active_sessions": len(self._sessions),
            "max_sessions": self._max_sessions,
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
        self, *, path: str, backend: str, name: str
    ) -> dict[str, Any]:
        directory = self._resolve_directory(path)
        if backend not in ("claude", "codex"):
            raise ValueError(f"unsupported backend: {backend}")
        return await self._start_tmux_agent(
            workdir=directory,
            backend=backend,
            name=name,
            command=self._agent_command(backend),
        )

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
        self, *, workdir: Path, backend: str, name: str, command: str
    ) -> dict[str, Any]:
        if backend not in ("claude", "codex"):
            raise ValueError(f"unsupported backend: {backend}")
        if not self._workdir.is_dir():
            raise ValueError(f"worker workdir does not exist: {self._workdir}")
        await self._recover_sessions(reconcile=True)
        if self._max_sessions and len(self._sessions) >= self._max_sessions:
            raise RuntimeError(f"worker session capacity reached ({self._max_sessions})")
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
        session_id = str(uuid.uuid4())
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
        await self._wait_ready(window_id, backend)
        self._sessions[session_id] = _TmuxWorkerSession(
            session_id=session_id,
            window_id=window_id,
            backend=backend,
        )
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
        return {"ok": code == 0, "error": "" if code == 0 else stderr.strip()}

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
            await self._bind_transcript(session)
            path = session.transcript_path
            if path is None:
                continue
            try:
                chunk, end_offset = await asyncio.to_thread(
                    self._read_transcript_tail, path, session.transcript_offset
                )
            except OSError:
                continue
            if not chunk or not chunk.endswith(b"\n"):
                continue
            session.transcript_offset = end_offset
            rows = []
            for raw_line in chunk.decode("utf-8", errors="replace").splitlines():
                row = TranscriptParser.parse_line(raw_line)
                if row:
                    rows.append(row)
            parsed, remaining = TranscriptParser.parse_entries(
                rows, pending_tools=session.pending_tools
            )
            session.pending_tools = remaining
            for entry in parsed:
                if entry.role != "assistant" or not entry.text:
                    continue
                events.append(
                    {
                        "event_type": "session_message",
                        "session_id": session.session_id,
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
        if session.transcript_path is not None:
            return

        def lookup() -> Path | None:
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
                if transcript.is_file():
                    return transcript
            return None

        path = await asyncio.to_thread(lookup)
        if path is None:
            return
        session.transcript_path = path
        try:
            session.transcript_offset = path.stat().st_size
        except OSError:
            session.transcript_offset = 0

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

    def _agent_command(self, backend: str, context_path: str = "") -> str:
        if backend == "codex":
            command = self._codex_command
            flags = self._codex_flags
        else:
            command = self._claude_command
            flags = self._claude_flags
        parts = [command]
        if flags:
            parts.append(flags)
        if context_path:
            prompt = (
                "Read the complete transferred session context from "
                f"{context_path}. Continue the conversation from that context."
            )
            parts.append(shlex.quote(prompt))
        return " ".join(parts)

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

    async def _wait_ready(self, window_id: str, backend: str) -> None:
        del backend
        deadline = asyncio.get_running_loop().time() + self._ready_timeout
        while asyncio.get_running_loop().time() < deadline:
            code, stdout, _stderr = await self._run_tmux(
                "capture-pane", "-p", "-t", window_id
            )
            pane = stdout if code == 0 else ""
            if pane and parse_status_line(pane) is None:
                tail = pane.splitlines()[-8:]
                if any(line.lstrip().startswith((">", "›", "❯")) for line in tail):
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


def _relay_address(raw: str) -> tuple[str, int, Any]:
    raw = raw.strip()
    if not raw:
        raise RuntimeError("CCBOT_NODE_RELAY_URL is required for node-agent")
    parsed = urlparse(raw if "://" in raw else f"tcp://{raw}")
    host = parsed.hostname
    port = parsed.port
    if not host or port is None:
        raise RuntimeError("CCBOT_NODE_RELAY_URL must include host and port")
    use_tls = parsed.scheme in ("tls", "ssl", "https")
    return host, port, ssl_module.create_default_context() if use_tls else None


def _default_node_id() -> str:
    value = "".join(
        char.lower() if char.isalnum() else "-" for char in platform.node()
    ).strip("-")
    if not value or value == "local":
        return f"worker-{secrets.token_hex(4)}"
    return value


def _print_connection_receipt(
    *, node_id: str, display_name: str, leader_id: str, relay_url: str
) -> None:
    """Print safe machine-readable facts for an SSH/remote-agent caller."""
    print("ccbot-node-agent: connected", flush=True)
    print(f"node_id={node_id}", flush=True)
    print(f"display_name={display_name}", flush=True)
    print(f"leader_id={leader_id}", flush=True)
    print(f"relay_url={relay_url}", flush=True)


async def _run_agent_forever(
    *,
    pairing_link: str = "",
    node_id_override: str = "",
    name_override: str = "",
    credential_file_override: str = "",
) -> None:
    invitation = PairingInvitation.from_link(pairing_link) if pairing_link else None
    relay_url = (
        invitation.relay_url
        if invitation is not None
        else os.environ.get("CCBOT_NODE_RELAY_URL", "")
    )
    host, port, ssl_context = _relay_address(relay_url)
    node_id = (
        node_id_override.strip()
        or (invitation.node_id if invitation is not None else "")
        or os.environ.get("CCBOT_NODE_ID", "").strip()
        or _default_node_id()
    )
    secret = (
        invitation.secret
        if invitation is not None
        else os.environ.get("CCBOT_NODE_SECRET", "")
    )
    leader_id = (
        invitation.leader_id
        if invitation is not None
        else os.environ.get("CCBOT_NODE_LEADER_ID", "local").strip()
    )
    credential_store = NodeCredentialStore(
        credential_file_override
        or os.environ.get(
            "CCBOT_NODE_CREDENTIAL_FILE",
            str(Path.home() / ".ccbot-worker" / "node-credential.json"),
        )
    )
    stored_secret = credential_store.load(
        node_id=node_id, leader_id=leader_id, relay_url=relay_url
    )
    using_pairing = invitation is not None and not stored_secret
    if stored_secret:
        secret = stored_secret
    if not node_id or not secret:
        raise RuntimeError("CCBOT_NODE_ID and CCBOT_NODE_SECRET are required")
    executor = TmuxWorkerExecutor(
        workdir=os.environ.get("CCBOT_WORKER_WORKDIR", str(Path.home())),
        tmux_session=os.environ.get("CCBOT_WORKER_TMUX_SESSION", "ccbot-worker"),
        claude_command=os.environ.get("CCBOT_CLAUDE_COMMAND", "claude"),
        codex_command=os.environ.get("CCBOT_CODEX_COMMAND", "codex"),
        claude_flags=os.environ.get("CCBOT_CLAUDE_FLAGS", ""),
        codex_flags=os.environ.get("CCBOT_CODEX_FLAGS", ""),
        ready_timeout=float(os.environ.get("CCBOT_WORKER_READY_TIMEOUT", "120")),
        context_limit_bytes=int(
            os.environ.get("CCBOT_WORKER_CONTEXT_LIMIT_BYTES", "0")
        ),
        max_sessions=int(os.environ.get("CCBOT_WORKER_MAX_SESSIONS", "8")),
    )
    context_dir = os.environ.get(
        "CCBOT_NODE_CONTEXT_DIR", str(Path.home() / ".ccbot-worker" / "contexts")
    )
    backends = tuple(
        value.strip()
        for value in os.environ.get("CCBOT_NODE_BACKENDS", "").split(",")
        if value.strip() in ("claude", "codex")
    )
    if not backends:
        backends = ("claude", "codex")
    display_name = (
        name_override.strip()
        or os.environ.get("CCBOT_NODE_NAME", node_id).strip()
        or node_id
    )
    agent: NodeAgent | None = None
    receipt_printed = False
    while True:
        try:
            transport = await connect_relay(
                host,
                port,
                node_id=node_id,
                role="worker",
                secret=secret,
                leader_id=leader_id,
                ssl=ssl_context,
                pairing_nonce=(
                    invitation.nonce if invitation is not None and using_pairing else ""
                ),
                pairing_expires=(
                    invitation.expires_at
                    if invitation is not None and using_pairing
                    else 0.0
                ),
            )
            if using_pairing and invitation is not None and transport.reconnect_secret:
                secret = transport.reconnect_secret
                credential_store.save(
                    node_id=node_id,
                    leader_id=leader_id,
                    relay_url=relay_url,
                    secret=secret,
                )
                invitation = None
                using_pairing = False
            if not receipt_printed:
                _print_connection_receipt(
                    node_id=node_id,
                    display_name=display_name,
                    leader_id=leader_id,
                    relay_url=relay_url,
                )
                receipt_printed = True
            if agent is None:
                agent = NodeAgent(
                    transport,
                    executor,
                    context_dir=context_dir,
                    node_id=node_id,
                    display_name=display_name,
                    backends=backends,
                )
            else:
                agent.attach_transport(transport)
            await agent.run()
        except asyncio.CancelledError:
            raise
        except PermissionError as exc:
            if (
                not using_pairing
                and invitation is not None
                and invitation.expires_at > time.time()
            ):
                logger.warning("stored node credential rejected; retrying pairing")
                secret = invitation.secret
                using_pairing = True
                continue
            logger.warning("node-agent relay authentication failed: %s", exc)
            await asyncio.sleep(2.0)
        except Exception as exc:
            logger.warning("node-agent relay disconnected: %s", exc)
            await asyncio.sleep(2.0)


async def _install_service_from_invitation(
    *,
    pairing_link: str,
    node_id_override: str,
    name_override: str,
    credential_file: str,
) -> Path:
    invitation = PairingInvitation.from_link(pairing_link)
    node_id = node_id_override.strip() or invitation.node_id
    display_name = name_override.strip() or node_id
    host, port, ssl_context = _relay_address(invitation.relay_url)
    path = Path(
        credential_file or Path.home() / ".ccbot-worker" / "node-credential.json"
    ).expanduser()
    store = NodeCredentialStore(path)
    stored_secret = store.load(
        node_id=node_id,
        leader_id=invitation.leader_id,
        relay_url=invitation.relay_url,
    )
    transport = None
    if stored_secret:
        try:
            transport = await connect_relay(
                host,
                port,
                node_id=node_id,
                role="worker",
                secret=stored_secret,
                leader_id=invitation.leader_id,
                ssl=ssl_context,
            )
        except PermissionError:
            transport = None
    if transport is None:
        transport = await connect_relay(
            host,
            port,
            node_id=node_id,
            role="worker",
            secret=invitation.secret,
            leader_id=invitation.leader_id,
            ssl=ssl_context,
            pairing_nonce=invitation.nonce,
            pairing_expires=invitation.expires_at,
        )
    try:
        if transport.reconnect_secret:
            store.save(
                node_id=node_id,
                leader_id=invitation.leader_id,
                relay_url=invitation.relay_url,
                secret=transport.reconnect_secret,
            )
        elif not stored_secret:
            raise RuntimeError("relay did not issue a reconnect credential")
    finally:
        await transport.close()
    from .node_service import install_node_service

    return install_node_service(
        node_id=node_id,
        display_name=display_name,
        relay_url=invitation.relay_url,
        leader_id=invitation.leader_id,
        credential_file=path,
    )


def main(argv: list[str] | None = None) -> None:
    """Run the Telegram-free worker process or bootstrap it from a link."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pairing",
        dest="pairing_link",
        help="one-time ccbot-node pairing link from the leader",
    )
    parser.add_argument("--node-id", default="", help="stable worker node id")
    parser.add_argument("--name", default="", help="worker display name")
    parser.add_argument("--credential-file", default="")
    parser.add_argument("--install-service", action="store_true")
    args = parser.parse_args(argv)
    if args.install_service:
        if not args.pairing_link:
            parser.error("--install-service requires --pairing")
        path = asyncio.run(
            _install_service_from_invitation(
                pairing_link=args.pairing_link,
                node_id_override=args.node_id,
                name_override=args.name,
                credential_file=args.credential_file,
            )
        )
        print("ccbot-node-agent: service active", flush=True)
        print(f"service_file={path}", flush=True)
        return
    asyncio.run(
        _run_agent_forever(
            pairing_link=args.pairing_link or "",
            node_id_override=args.node_id,
            name_override=args.name,
            credential_file_override=args.credential_file,
        )
    )


__all__ = [
    "NodeAgent",
    "NodeCredentialStore",
    "TmuxWorkerExecutor",
    "WorkerSessionExecutor",
    "main",
]
