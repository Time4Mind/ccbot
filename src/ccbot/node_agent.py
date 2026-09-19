"""Relay worker agent without Telegram or leader-state dependencies."""

from __future__ import annotations

import asyncio
import argparse
import base64
import hashlib
import logging
import os
import secrets
import shlex
import ssl as ssl_module
import platform
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
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

logger = logging.getLogger(__name__)
HEALTH_INTERVAL_SECONDS = 15.0


class WorkerSessionExecutor(Protocol):
    async def start_context_session(
        self, *, context_path: str, backend: str, name: str
    ) -> dict[str, Any]: ...

    async def send_text(self, *, session_id: str, text: str) -> dict[str, Any]: ...


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
        heartbeat = asyncio.create_task(self._health_loop(), name="node-health")
        try:
            while True:
                message = await self._transport.receive()
                if message.kind != "command":
                    continue
                await self._handle_command(message)
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    async def _send_health(self) -> None:
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
                    "capabilities": {"context_transfer": True, "send_text": True},
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

    async def _handle_command(self, message: NodeEnvelope) -> None:
        if not message.request_id:
            await self._send_error(message, "request_id is required")
            return
        receipt = self._ledger.accept(message.request_id)
        await self._transport.send(
            NodeEnvelope(
                kind="ack",
                request_id=message.request_id,
                payload={"accepted": receipt.accepted},
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
        if operation == "send_text":
            session_id = str(payload.get("session_id", ""))
            text = str(payload.get("text", ""))
            if not session_id or not text:
                raise ValueError("session_id and text are required")
            return await self._executor.send_text(session_id=session_id, text=text)
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
    ):
        self._workdir = Path(workdir).expanduser().resolve()
        self._tmux_session = tmux_session
        self._claude_command = claude_command
        self._codex_command = codex_command
        self._claude_flags = claude_flags
        self._codex_flags = codex_flags
        self._ready_timeout = ready_timeout
        self._context_limit_bytes = max(0, context_limit_bytes)
        self._sessions: dict[str, _TmuxWorkerSession] = {}

    async def start_context_session(
        self, *, context_path: str, backend: str, name: str
    ) -> dict[str, Any]:
        if backend not in ("claude", "codex"):
            raise ValueError(f"unsupported backend: {backend}")
        if not self._workdir.is_dir():
            raise ValueError(f"worker workdir does not exist: {self._workdir}")
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
            str(self._workdir),
        )
        if code != 0:
            raise RuntimeError(stderr.strip() or "tmux new-window failed")
        window_id = stdout.strip()
        if not window_id:
            raise RuntimeError("tmux did not return a window id")
        command = self._agent_command(backend, context_path)
        code, _stdout, stderr = await self._run_tmux(
            "send-keys", "-t", window_id, command, "C-m"
        )
        if code != 0:
            raise RuntimeError(stderr.strip() or "tmux send-keys failed")
        await self._wait_ready(window_id, backend)
        session_id = str(uuid.uuid4())
        self._sessions[session_id] = _TmuxWorkerSession(
            session_id=session_id,
            window_id=window_id,
            backend=backend,
        )
        return {
            "target_window_id": window_id,
            "target_workdir": str(self._workdir),
            "target_agent_session_id": session_id,
            "context_path": context_path,
            "context_error": (
                "The transferred context exceeds the worker provider limit"
                if self._context_limit_bytes
                and Path(context_path).stat().st_size > self._context_limit_bytes
                else ""
            ),
        }

    async def send_text(self, *, session_id: str, text: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "worker session not found"}
        ok = await tmux_input_transport.send_literal_chunked(
            session.window_id, text, backend=session.backend
        )
        return {"ok": ok, "error": "" if ok else "tmux input failed"}

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

    def _agent_command(self, backend: str, context_path: str) -> str:
        if backend == "codex":
            command = self._codex_command
            flags = self._codex_flags
        else:
            command = self._claude_command
            flags = self._claude_flags
        prompt = (
            "Read the complete transferred session context from "
            f"{context_path}. Continue the conversation from that context."
        )
        parts = [command]
        if flags:
            parts.append(flags)
        parts.append(shlex.quote(prompt))
        return " ".join(parts)

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
    *, pairing_link: str = "", node_id_override: str = "", name_override: str = ""
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
                pairing_nonce=invitation.nonce if invitation is not None else "",
                pairing_expires=(
                    invitation.expires_at if invitation is not None else 0.0
                ),
            )
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
        except Exception as exc:
            logger.warning("node-agent relay disconnected: %s", exc)
            await asyncio.sleep(2.0)


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
    args = parser.parse_args(argv)
    asyncio.run(
        _run_agent_forever(
            pairing_link=args.pairing_link or "",
            node_id_override=args.node_id,
            name_override=args.name,
        )
    )


__all__ = ["NodeAgent", "TmuxWorkerExecutor", "WorkerSessionExecutor", "main"]
