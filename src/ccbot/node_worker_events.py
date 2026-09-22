"""Incremental worker transcript polling with durable replay cursors."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from .node_history import serialize_entry
from .transcript_parser import TranscriptParser
from .utils import ccbot_dir

logger = logging.getLogger(__name__)
MAX_LIVE_EVENT_BYTES = 2 * 1024 * 1024
_OMITTED_IMAGE_TEXT = "[Image omitted: live event exceeds 2 MiB limit]"


def _bound_live_payload(payload: dict[str, Any]) -> dict[str, Any]:
    def encoded_size() -> int:
        return len(json.dumps(payload, ensure_ascii=False).encode())

    if encoded_size() <= MAX_LIVE_EVENT_BYTES:
        return payload
    if payload.get("image_data"):
        payload["image_data"] = []
        payload["image_truncated"] = True
        if not str(payload.get("text", "")).strip():
            payload["text"] = _OMITTED_IMAGE_TEXT
    text = str(payload.get("text", ""))
    while text and encoded_size() > MAX_LIVE_EVENT_BYTES:
        text = text[len(text) // 2 :]
        payload["text"] = text
        payload["payload_truncated"] = True
    return payload


class WorkerEventPollingMixin:
    _sessions: dict[str, Any]
    _reconcile_interval: float
    _last_reconcile_at: float
    _event_cursor_for: Callable[[str], int | None] | None
    _ensure_event_cursor: Callable[[str, int], None] | None

    def set_event_cursor_store(
        self,
        cursor_for: Callable[[str], int | None],
        ensure_cursor: Callable[[str, int], None],
    ) -> None:
        self._event_cursor_for = cursor_for
        self._ensure_event_cursor = ensure_cursor

    async def _recover_sessions(self, *, reconcile: bool = False) -> None:
        raise NotImplementedError

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
            session_event_start = len(events)
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
                if not chunk:
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
            session.transcript_offset = end_offset
            session.pending_tools = remaining
            for entry in parsed:
                if entry.role not in ("user", "assistant") or not (
                    entry.text or entry.image_data
                ):
                    continue
                payload = {
                    "event_type": "session_message",
                    "session_id": session.session_id,
                    **serialize_entry(entry),
                }
                events.append(_bound_live_payload(payload))
            if len(events) > session_event_start:
                events[-1]["_transcript_path"] = str(path)
                events[-1]["_transcript_offset"] = end_offset
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
        complete_end = chunk.rfind(b"\n") + 1
        if complete_end <= 0:
            return b"", start
        return chunk[:complete_end], start + complete_end

    async def _bind_transcript(self, session: Any) -> None:
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
        if provider_session_id == session.ignored_provider_session_id:
            return
        session.ignored_provider_session_id = ""
        session.transcript_path = path
        session.provider_session_id = provider_session_id
        session.provider_transcript_path = str(path)
        try:
            stored_cursor = (
                self._event_cursor_for(str(path)) if self._event_cursor_for else None
            )
            session.transcript_offset = (
                stored_cursor
                if stored_cursor is not None
                else path.stat().st_size
                if getattr(session, "recovered", False)
                else 0
            )
            if self._ensure_event_cursor is not None:
                self._ensure_event_cursor(str(path), session.transcript_offset)
        except OSError:
            session.transcript_offset = 0
        logger.info(
            "worker transcript bound session=%s window=%s offset=%d recovered=%s",
            session.session_id,
            session.window_id,
            session.transcript_offset,
            getattr(session, "recovered", False),
        )


__all__ = ["WorkerEventPollingMixin"]
