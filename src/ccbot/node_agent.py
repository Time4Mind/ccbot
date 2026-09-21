"""Relay worker agent without Telegram or leader-state dependencies."""

from __future__ import annotations

import asyncio
import argparse
import base64
import hashlib
import getpass
import json
import logging
import os
import secrets
import ssl as ssl_module
import platform
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine, Protocol, cast
from urllib.parse import urlparse

from .node_transport import (
    NodeEnvelope,
    NodeTransport,
    RequestReceiptLedger,
    connect_relay,
)
from .node_pairing import PairingInvitation
from .node_event_pump import NodeEventPump
from .node_update import GitNodeUpdater, current_git_revision
from .node_inbox import dispatch_inbox_operation, is_inbox_operation
from .node_session_start import dispatch_create_session
from .node_worker import TmuxWorkerExecutor

logger = logging.getLogger(__name__)
_CONTROL_OPERATIONS = {
    "send_key",
    "capture_session",
    "terminate_session",
    "cancel_session_start",
    "health",
}
_STARTUP_OPERATIONS = {"create_session", "transfer_context_finish"}
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
        self,
        *,
        path: str,
        backend: str,
        name: str,
        startup_id: str = "",
        resume_session_id: str = "",
        source_backend: str = "",
        provider_transcript_path: str = "",
    ) -> dict[str, Any]: ...
    async def cancel_session_start(self, *, startup_id: str) -> dict[str, Any]: ...
    async def start_context_session(
        self, *, context_path: str, backend: str, name: str
    ) -> dict[str, Any]: ...
    async def send_text(self, *, session_id: str, text: str) -> dict[str, Any]: ...
    async def send_key(self, *, session_id: str, key: str) -> dict[str, Any]: ...
    async def capture_session(self, *, session_id: str) -> dict[str, Any]: ...
    async def terminate_session(self, *, session_id: str) -> dict[str, Any]: ...


class RuntimeUpdater(Protocol):
    async def update(self, revision: str) -> dict[str, object]: ...


async def _restart_current_process() -> None:
    await asyncio.sleep(0.2)
    os.execv(sys.executable, [sys.executable, *sys.argv])


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
        runtime_revision: str | None = None,
        runtime_updater: RuntimeUpdater | None = None,
        restart_callback: Callable[[], Coroutine[Any, Any, None]] | None = None,
        ssh_access: dict[str, Any] | None = None,
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
        self._runtime_revision = (
            current_git_revision() if runtime_revision is None else runtime_revision
        )
        self._runtime_updater = runtime_updater or GitNodeUpdater()
        self._restart_callback = restart_callback or _restart_current_process
        self._ssh_access = dict(ssh_access or {})
        self._boot_id = secrets.token_urlsafe(12)
        self._startup_tasks: set[asyncio.Task[None]] = set()
        self._event_pump = NodeEventPump(
            node_id, available=callable(getattr(executor, "poll_events", None))
        )

    def attach_transport(self, transport: NodeTransport) -> None:
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
                if operation in _CONTROL_OPERATIONS:
                    queue = control_commands
                else:
                    queue = regular_commands
                await queue.put(message)
        finally:
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)
            startup_tasks = tuple(self._startup_tasks)
            for task in startup_tasks:
                task.cancel()
            await asyncio.gather(*startup_tasks, return_exceptions=True)

    async def _command_worker(self, queue: asyncio.Queue[NodeEnvelope]) -> None:
        while True:
            message = await queue.get()
            try:
                operation = str((message.payload or {}).get("operation", ""))
                if operation in _STARTUP_OPERATIONS:
                    task = asyncio.create_task(
                        self._handle_command(message),
                        name=f"node-startup:{message.request_id}",
                    )
                    self._startup_tasks.add(task)
                    task.add_done_callback(self._startup_tasks.discard)
                else:
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
                    "state": (
                        "ready"
                        if self._backends and self._event_pump.healthy
                        else "online"
                    ),
                    "platform": platform.system(),
                    "arch": platform.machine(),
                    "backends": list(self._backends),
                    "capabilities": {
                        "directory_browser": True,
                        "create_session": True,
                        "context_transfer": True,
                        "send_text": True,
                        "inbox_upload": True,
                        "file_download": True,
                        "event_stream": self._event_pump.healthy,
                    },
                    "capacity": capacity,
                    "ccbot_version": self._runtime_revision,
                    "boot_id": self._boot_id,
                    "ssh": dict(self._ssh_access),
                },
            )
        )

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(15.0)
            try:
                await self._send_health()
            except (ConnectionError, asyncio.CancelledError):
                return

    async def _event_loop(self) -> None:
        poll_events = getattr(self._executor, "poll_events", None)
        if not callable(poll_events):
            return
        await self._event_pump.run(
            cast(Callable[[], Awaitable[list[dict[str, Any]]]], poll_events),
            transport=lambda: self._transport,
            publish_health=self._send_health,
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
        except asyncio.CancelledError:
            self._ledger.complete(
                message.request_id,
                {"ok": False, "error": "worker command cancelled after disconnect"},
            )
            raise
        except Exception as exc:
            logger.exception(
                "node command failed operation=%s",
                (message.payload or {}).get("operation"),
            )
            result = {"ok": False, "error": str(exc).strip() or type(exc).__name__}
        self._ledger.complete(message.request_id, result)
        await self._transport.send(
            NodeEnvelope(kind="result", request_id=message.request_id, payload=result)
        )
        if result.get("ok") and result.get("restart_required"):
            asyncio.create_task(self._restart_callback(), name="node-runtime-restart")

    async def _dispatch(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation", ""))
        if operation == "transfer_context_begin":
            return self._begin_context(payload)
        if operation == "transfer_context_chunk":
            return self._append_context(payload)
        if operation == "transfer_context_finish":
            return await self._finish_context(payload)
        if is_inbox_operation(operation):
            return await dispatch_inbox_operation(self._executor, payload)
        if operation == "list_directories":
            return await self._executor.list_directories(
                path=str(payload.get("path", ""))
            )
        if operation == "create_directory":
            return await self._executor.create_directory(
                path=str(payload.get("path", "")),
                name=str(payload.get("name", "")),
            )
        if operation in ("create_session", "restore_session"):
            return await dispatch_create_session(self._executor, payload)
        if operation == "resolve_provider_session":
            return await cast(Any, self._executor).resolve_provider_session(
                path=str(payload.get("path", "")),
                backend=str(payload.get("backend", "")),
                session_id=str(payload.get("session_id", "")),
            )
        if operation == "cancel_session_start":
            return await self._executor.cancel_session_start(
                startup_id=str(payload.get("startup_id", ""))
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
            kwargs: dict[str, Any] = {"session_id": str(payload.get("session_id", ""))}
            if "with_ansi" in payload:
                kwargs["with_ansi"] = bool(payload["with_ansi"])
            return await self._executor.capture_session(**kwargs)
        if operation == "terminate_session":
            return await self._executor.terminate_session(
                session_id=str(payload.get("session_id", ""))
            )
        if operation == "health":
            return {"ok": True}
        if operation == "update_runtime":
            return await self._runtime_updater.update(str(payload.get("revision", "")))
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


def _ssh_access_from_environment() -> dict[str, Any]:
    """Read public connection metadata; authentication stays in OpenSSH."""
    host = os.environ.get("CCBOT_NODE_SSH_HOST", "").strip()
    if not host:
        return {}
    user = os.environ.get("CCBOT_NODE_SSH_USER", "").strip() or getpass.getuser()
    try:
        port = int(os.environ.get("CCBOT_NODE_SSH_PORT", "22"))
    except ValueError as exc:
        raise RuntimeError("CCBOT_NODE_SSH_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("CCBOT_NODE_SSH_PORT must be between 1 and 65535")
    return {
        "host": host,
        "user": user,
        "port": port,
        "proxy_jump": os.environ.get("CCBOT_NODE_SSH_PROXY_JUMP", "").strip(),
    }


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
    ssh_access = _ssh_access_from_environment()
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
                    ssh_access=ssh_access,
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


__all__ = ["NodeAgent", "NodeCredentialStore", "main"]
