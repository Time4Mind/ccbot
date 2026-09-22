"""Durable, acknowledged worker-to-leader session event delivery."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .node_transport import NodeEnvelope, NodeTransport

logger = logging.getLogger(__name__)


class NodeEventPump:
    """Retain ordered events on disk until the leader acknowledges apply."""

    def __init__(
        self,
        node_id: str,
        *,
        available: bool = True,
        state_path: str | Path | None = None,
        poll_interval: float = 0.5,
    ):
        self.node_id = node_id
        self._state_path = Path(state_path).expanduser() if state_path else None
        self._poll_interval = max(0.01, poll_interval)
        self.pending: deque[dict[str, Any]] = deque()
        self._next_sequence = 1
        self._stream_id = secrets.token_urlsafe(12)
        self._cursors: dict[str, int] = {}
        self.healthy = available
        self._load()

    def cursor_for(self, transcript_path: str) -> int | None:
        return self._cursors.get(transcript_path)

    def ensure_cursor(self, transcript_path: str, offset: int) -> None:
        if transcript_path in self._cursors:
            return
        self._cursors[transcript_path] = max(0, offset)
        self._save()

    def acknowledge(self, event_id: str) -> bool:
        if not self.pending or self.pending[0].get("event_id") != event_id:
            return False
        item = self.pending.popleft()
        payload = item.get("payload", {})
        transcript_path = str(payload.get("_transcript_path", ""))
        try:
            transcript_offset = max(0, int(payload.get("_transcript_offset", 0) or 0))
        except (ValueError, TypeError):
            transcript_offset = 0
        if transcript_path:
            self._cursors[transcript_path] = max(
                transcript_offset, self._cursors.get(transcript_path, 0)
            )
        self._save()
        return True

    def _load(self) -> None:
        if self._state_path is None:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            pending = data.get("pending", [])
            next_sequence = int(data.get("next_sequence", 1))
        except (OSError, ValueError, TypeError, AttributeError):
            return
        loaded_pending: list[dict[str, Any]] = []
        previous_sequence = 0
        valid_pending = isinstance(pending, list)
        if valid_pending:
            for item in pending:
                try:
                    event_id = str(item["event_id"])
                    sequence = int(item["sequence"])
                    payload = item["payload"]
                except (KeyError, ValueError, TypeError):
                    valid_pending = False
                    break
                if (
                    not event_id
                    or sequence < 1
                    or not isinstance(payload, dict)
                    or (previous_sequence and sequence != previous_sequence + 1)
                ):
                    valid_pending = False
                    break
                loaded_pending.append(
                    {"event_id": event_id, "sequence": sequence, "payload": payload}
                )
                previous_sequence = sequence
        if valid_pending:
            self.pending.extend(loaded_pending)
            self._next_sequence = max(1, next_sequence, previous_sequence + 1)
            self._stream_id = str(data.get("stream_id", "")) or self._stream_id
        else:
            logger.warning("discarding invalid worker event outbox state")
        cursors = data.get("cursors", {})
        if isinstance(cursors, dict):
            for path, offset in cursors.items():
                try:
                    normalized_path = str(path)
                    normalized_offset = max(0, int(offset))
                except (ValueError, TypeError):
                    continue
                if normalized_path:
                    self._cursors[normalized_path] = normalized_offset

    def _save(self) -> None:
        if self._state_path is None:
            return
        path = self._state_path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "next_sequence": self._next_sequence,
                    "stream_id": self._stream_id,
                    "cursors": self._cursors,
                    "pending": list(self.pending),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)

    def _enqueue(self, payloads: list[dict[str, Any]]) -> None:
        for payload in payloads:
            sequence = self._next_sequence
            self._next_sequence += 1
            self.pending.append(
                {
                    "event_id": f"{self.node_id}:{sequence}",
                    "sequence": sequence,
                    "payload": dict(payload),
                }
            )
        if payloads:
            self._save()

    async def run(
        self,
        poll_events: Callable[[], Awaitable[list[dict[str, Any]]]],
        *,
        transport: Callable[[], NodeTransport],
        publish_health: Callable[[], Awaitable[None]],
    ) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                if not self.pending:
                    self._enqueue(await poll_events())
                if self.pending:
                    item = self.pending[0]
                    await transport().send(
                        NodeEnvelope(
                            kind="event",
                            request_id=str(item["event_id"]),
                            sequence=int(item["sequence"]),
                            payload={
                                "node_id": self.node_id,
                                "event_stream_id": self._stream_id,
                                **{
                                    key: value
                                    for key, value in dict(item["payload"]).items()
                                    if not key.startswith("_")
                                },
                            },
                        )
                    )
                recovered = not self.healthy
                self.healthy = True
                if recovered:
                    logger.info("node event pump recovered node=%s", self.node_id)
                    await publish_health()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.healthy = False
                item = self.pending[0] if self.pending else {}
                payload = item.get("payload", {})
                logger.exception(
                    "node event pump failed node=%s session=%s pending=%d: %s",
                    self.node_id,
                    payload.get("session_id", ""),
                    len(self.pending),
                    exc,
                )
                try:
                    await publish_health()
                except Exception:
                    logger.debug(
                        "node event pump could not publish degraded health node=%s",
                        self.node_id,
                    )


__all__ = ["NodeEventPump"]
