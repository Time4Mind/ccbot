"""Bounded, atomic inbox transfer shared by leader and worker runtimes."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


MAX_INBOX_BYTES = 20 * 1024 * 1024
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_name(value: str) -> str:
    name = _SAFE_NAME_RE.sub("_", Path(value).name.strip().replace(" ", "_"))
    if not name or name in (".", ".."):
        name = "file"
    if len(name) > 80:
        stem, suffix = os.path.splitext(name)
        name = stem[: 80 - len(suffix)] + suffix
    return name


class RemoteInboxMixin:
    """Leader methods mixed into ``RemoteNodeRuntime``."""

    async def upload_inbox_file(
        self,
        target_node_id: str,
        session_id: str,
        filename: str,
        content: bytes,
    ) -> dict[str, Any]:
        owner = cast(Any, self)
        if len(content) > MAX_INBOX_BYTES:
            raise ValueError("inbox file exceeds worker transfer limit")
        upload_id = secrets.token_urlsafe(18)
        payload = {
            "upload_id": upload_id,
            "session_id": session_id,
            "filename": filename,
            "total_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        try:
            begin = await owner._request(target_node_id, "upload_inbox_begin", payload)
            owner._require_ok(begin)
            upload_id = str(begin.get("upload_id", upload_id))
            for index, start in enumerate(range(0, len(content), owner._chunk_size)):
                chunk = content[start : start + owner._chunk_size]
                result = await owner._request(
                    target_node_id,
                    "upload_inbox_chunk",
                    {
                        "upload_id": upload_id,
                        "chunk_index": index,
                        "data": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                owner._require_ok(result)
            finished = await owner._request(
                target_node_id, "upload_inbox_finish", {"upload_id": upload_id}
            )
            owner._require_ok(finished)
            return finished
        except BaseException:
            try:
                await owner._request(
                    target_node_id, "upload_inbox_abort", {"upload_id": upload_id}
                )
            except BaseException:
                pass
            raise

    async def inspect_session(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]:
        return await cast(Any, self)._request(
            target_node_id, "inspect_session", {"session_id": session_id}
        )

    async def reset_session_binding(
        self, target_node_id: str, session_id: str
    ) -> dict[str, Any]:
        return await cast(Any, self)._request(
            target_node_id, "reset_session_binding", {"session_id": session_id}
        )


@dataclass
class _PendingUpload:
    path: Path
    final_path: Path
    expected_bytes: int
    expected_sha256: str
    next_chunk: int = 0
    received_bytes: int = 0


class WorkerInboxMixin:
    """Worker operations mixed into the tmux executor."""

    def _upload_state(self) -> tuple[dict[str, _PendingUpload], dict[str, Path]]:
        if not hasattr(self, "_pending_inbox_uploads"):
            self._pending_inbox_uploads: dict[str, _PendingUpload] = {}
            self._completed_inbox_uploads: dict[str, Path] = {}
        return self._pending_inbox_uploads, self._completed_inbox_uploads

    async def inspect_session(self, *, session_id: str) -> dict[str, Any]:
        session = await cast(Any, self)._find_session(session_id)
        if session is None:
            return {"ok": True, "found": False}
        return {
            "ok": True,
            "found": True,
            "window_id": session.window_id,
            "workdir": str(session.workdir),
            "backend": session.backend,
        }

    async def reset_session_binding(self, *, session_id: str) -> dict[str, Any]:
        session = await cast(Any, self)._find_session(session_id)
        if session is None:
            return {"ok": False, "error": "worker session not found"}
        session.ignored_provider_session_id = session.provider_session_id
        session.provider_session_id = ""
        session.provider_transcript_path = ""
        session.transcript_path = None
        session.transcript_offset = 0
        session.pending_tools.clear()
        session.binding_announced = False
        return {"ok": True}

    async def upload_inbox_begin(self, **payload: Any) -> dict[str, Any]:
        upload_id = str(payload.get("upload_id", ""))
        session_id = str(payload.get("session_id", ""))
        expected_bytes = int(payload.get("total_bytes", -1))
        expected_sha256 = str(payload.get("sha256", ""))
        if not upload_id or not session_id:
            raise ValueError("upload_id and session_id are required")
        if expected_bytes < 0 or expected_bytes > MAX_INBOX_BYTES:
            raise ValueError("inbox file exceeds worker transfer limit")
        if len(expected_sha256) != 64:
            raise ValueError("inbox sha256 is required")
        session = await cast(Any, self)._find_session(session_id)
        if session is None:
            raise ValueError("worker session not found")
        pending, completed = self._upload_state()
        prior = completed.get(upload_id)
        if prior is not None and prior.is_file():
            return {"ok": True, "upload_id": upload_id, "completed": True}
        old = pending.pop(upload_id, None)
        if old is not None:
            old.path.unlink(missing_ok=True)
        inbox = Path(session.workdir).expanduser().resolve() / ".ccbot-inbox"
        inbox.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_name(str(payload.get("filename", "file")))
        final_path = inbox / f"{upload_id[:12]}-{safe_name}"
        temp_path = inbox / f".{upload_id}.part"
        temp_path.write_bytes(b"")
        pending[upload_id] = _PendingUpload(
            temp_path, final_path, expected_bytes, expected_sha256
        )
        return {"ok": True, "upload_id": upload_id}

    async def upload_inbox_chunk(self, **payload: Any) -> dict[str, Any]:
        upload_id = str(payload.get("upload_id", ""))
        pending, _completed = self._upload_state()
        upload = pending.get(upload_id)
        if upload is None:
            raise ValueError("unknown inbox upload")
        index = int(payload.get("chunk_index", -1))
        if index != upload.next_chunk:
            await self.upload_inbox_abort(upload_id=upload_id)
            raise ValueError("inbox chunk sequence mismatch")
        try:
            chunk = base64.b64decode(str(payload.get("data", "")), validate=True)
        except Exception as exc:
            await self.upload_inbox_abort(upload_id=upload_id)
            raise ValueError("invalid inbox chunk") from exc
        if upload.received_bytes + len(chunk) > upload.expected_bytes:
            await self.upload_inbox_abort(upload_id=upload_id)
            raise ValueError("inbox byte count exceeds declared size")
        with upload.path.open("ab") as stream:
            stream.write(chunk)
        upload.received_bytes += len(chunk)
        upload.next_chunk += 1
        return {"ok": True, "received_bytes": upload.received_bytes}

    async def upload_inbox_finish(self, **payload: Any) -> dict[str, Any]:
        upload_id = str(payload.get("upload_id", ""))
        pending, completed = self._upload_state()
        prior = completed.get(upload_id)
        if prior is not None and prior.is_file():
            return {"ok": True, "relative_path": f".ccbot-inbox/{prior.name}"}
        upload = pending.pop(upload_id, None)
        if upload is None:
            raise ValueError("unknown inbox upload")
        content = upload.path.read_bytes()
        if len(content) != upload.expected_bytes:
            upload.path.unlink(missing_ok=True)
            raise ValueError("inbox byte count mismatch")
        if hashlib.sha256(content).hexdigest() != upload.expected_sha256:
            upload.path.unlink(missing_ok=True)
            raise ValueError("inbox sha256 mismatch")
        upload.path.replace(upload.final_path)
        completed[upload_id] = upload.final_path
        return {
            "ok": True,
            "relative_path": f".ccbot-inbox/{upload.final_path.name}",
        }

    async def upload_inbox_abort(self, **payload: Any) -> dict[str, Any]:
        upload_id = str(payload.get("upload_id", ""))
        pending, _completed = self._upload_state()
        upload = pending.pop(upload_id, None)
        if upload is not None:
            upload.path.unlink(missing_ok=True)
        return {"ok": True}


async def dispatch_inbox_operation(
    executor: Any, payload: dict[str, Any]
) -> dict[str, Any]:
    operation = str(payload.get("operation", ""))
    if operation not in {
        "inspect_session",
        "reset_session_binding",
        "upload_inbox_begin",
        "upload_inbox_chunk",
        "upload_inbox_finish",
        "upload_inbox_abort",
    }:
        raise ValueError(f"unsupported inbox operation: {operation}")
    method = getattr(executor, operation)
    kwargs = {
        key: value
        for key, value in payload.items()
        if key not in ("operation", "target_node_id")
    }
    return await method(**kwargs)
