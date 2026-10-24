"""Persist final-answer obligations and retry them independently of UI tasks.

A send without an acknowledgement is held for explicit reconciliation. Edits
have a known Telegram identity and can safely be repeated after a lost reply.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable
import uuid

import httpx
from telegram.error import BadRequest, NetworkError, RetryAfter

from .config import config
from .utils import atomic_write_json

logger = logging.getLogger(__name__)
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)


def before_submission(exc: BaseException) -> bool:
    """Trust typed connection/pool failures, never a substring in an error."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
        ):
            return True
        current = current.__cause__
    return False


@dataclass
class FinalDelivery:
    id: str
    user_id: int
    session_id: str
    payload: dict[str, Any]
    status: str = "pending"
    attempts: int = 0
    retry_at: float = 0.0
    cooldown_until: float = 0.0
    message_id: int | None = None
    created_at: float = field(default_factory=time.time)


Driver = Callable[[FinalDelivery], Awaitable[bool | None]]


class FinalDeliveryQueue:
    """Disk-backed FIFO per session; lifecycle-owned workers survive UI resets."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: list[FinalDelivery] = []
        self.completed_keys: list[str] = []
        self.tasks: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._loaded = False
        self._driver: Driver | None = None
        self._ready: Callable[[FinalDelivery], bool] | None = None

    def load(self) -> None:
        if self._loaded:
            return
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = {"deliveries": []}
        # Fail closed on damaged state: silently overwriting it loses answers.
        records = [FinalDelivery(**row) for row in raw["deliveries"]]
        for item in records:
            if item.status == "preparing":
                item.status = "pending"
            if item.status == "in_flight":
                item.status = "pending" if item.message_id is not None else "unknown"
        self.records = records
        self.completed_keys = raw.get("completed_keys", [])
        self._loaded = True
        if records:
            self.save()

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "deliveries": [asdict(r) for r in self.records],
                "completed_keys": self.completed_keys,
            },
        )

    def pending(
        self, user_id: int, session_id: str | None = None
    ) -> list[FinalDelivery]:
        self.load()
        return [
            r
            for r in self.records
            if r.user_id == user_id
            and (session_id is None or r.session_id == session_id)
        ]

    def contains_event(self, event_key: str) -> bool:
        self.load()
        return event_key in self.completed_keys or any(
            row.payload.get("event_key") == event_key for row in self.records
        )

    def enqueue(
        self,
        user_id: int,
        session_id: str,
        payload: dict[str, Any],
        *,
        preparing: bool = False,
        event_key: str = "",
    ) -> FinalDelivery | None:
        self.load()
        if event_key:
            if event_key in self.completed_keys:
                return None
            existing = next(
                (r for r in self.records if r.payload.get("event_key") == event_key),
                None,
            )
            if existing is not None:
                return existing
            payload["event_key"] = event_key
        item = FinalDelivery(
            uuid.uuid4().hex[:16],
            user_id,
            session_id,
            payload,
            status="preparing" if preparing else "pending",
        )
        self.records.append(item)
        self.save()
        logger.info("final_delivery_queued id=%s session=%s", item.id, session_id)
        return item

    def bind_ack(self, delivery_id: str, message_id: int) -> None:
        """Checkpoint the known identity before any post-send UI bookkeeping."""
        for item in self.records:
            if item.id == delivery_id:
                item.message_id = message_id
                self.save()
                return

    def resolve(self, item: FinalDelivery) -> None:
        key = item.payload.get("event_key")
        if key:
            self.completed_keys = [*self.completed_keys[-511:], key]
        self.records.remove(item)
        self._locks.pop(item.id, None)
        self.save()
        logger.info(
            "final_delivery_confirmed id=%s session=%s", item.id, item.session_id
        )

    async def attempt(self, item: FinalDelivery, driver: Driver) -> bool | None:
        async with self._locks.setdefault(item.id, asyncio.Lock()):
            if item not in self.records:
                return True
            head = self.pending(item.user_id, item.session_id)[0]
            if (
                head is not item
                or item.status == "unknown"
                or time.time() < max(item.retry_at, item.cooldown_until)
            ):
                return False
            if self._ready is not None and not self._ready(item):
                return None
            item.status = "in_flight"
            self.save()
            try:
                delivered = await driver(item)
            except asyncio.CancelledError:
                item.status = "pending" if item.message_id is not None else "unknown"
                self.save()
                raise
            except RetryAfter as exc:
                wait = exc.retry_after
                seconds = (
                    wait.total_seconds() if isinstance(wait, timedelta) else float(wait)
                )
                item.cooldown_until = time.time() + max(0.0, seconds)
                delivered = False
            except Exception as exc:
                if (
                    item.message_id is None
                    and isinstance(exc, (NetworkError, TimeoutError))
                    and not isinstance(exc, BadRequest)
                    and not before_submission(exc)
                ):
                    item.status = "unknown"
                    self.save()
                    logger.error(
                        "final_delivery_unknown id=%s session=%s; reconcile with /delivery",
                        item.id,
                        item.session_id,
                    )
                    return False
                delivered = False
                logger.warning(
                    "final_delivery_failed id=%s error_type=%s",
                    item.id,
                    type(exc).__name__,
                )
            if item not in self.records:
                return True
            if delivered is None:
                item.status = "pending"
                self.save()
                return None
            if delivered:
                self.resolve(item)
                return True
            item.status = "pending"
            item.retry_at = (
                time.time() + RETRY_DELAYS[min(item.attempts, len(RETRY_DELAYS) - 1)]
            )
            item.attempts += 1
            self.save()
            logger.warning(
                "final_delivery_pending id=%s session=%s attempts=%s retry_at=%s",
                item.id,
                item.session_id,
                item.attempts,
                max(item.retry_at, item.cooldown_until),
            )
            return False

    def start(
        self, driver: Driver, *, ready: Callable[[FinalDelivery], bool] | None = None
    ) -> None:
        self.load()
        self._driver = driver
        self._ready = ready
        self.kick()

    def kick(self) -> None:
        if self._driver is None:
            return
        for item in self.records:
            key = (item.user_id, item.session_id)
            if key not in self.tasks or self.tasks[key].done():
                self.tasks[key] = asyncio.create_task(
                    self._run(key), name=f"final-delivery:{key[0]}:{key[1]}"
                )

    async def _run(self, key: tuple[int, str]) -> None:
        assert self._driver is not None
        while pending := self.pending(*key):
            item = pending[0]
            if item.status == "unknown":
                return
            if item.status == "preparing":
                await asyncio.sleep(0.05)
                continue
            wait = max(item.retry_at, item.cooldown_until) - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                outcome = await self.attempt(item, self._driver)
                if outcome is None:
                    await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("final_delivery_worker_failed session=%s", key[1])
                await asyncio.sleep(1.0)

    async def shutdown(self) -> None:
        self._driver = None
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()


final_delivery_queue = FinalDeliveryQueue(config.config_dir / "final-deliveries.json")
