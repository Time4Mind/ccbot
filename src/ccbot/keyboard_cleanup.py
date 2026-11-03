"""Durable identities of obsolete Telegram keyboards awaiting cleanup."""

from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from .config import config
from .utils import atomic_write_json


@dataclass
class CleanupItem:
    user_id: int
    message_id: int
    retry_at: float = 0.0
    attempts: int = 0


class KeyboardCleanup:
    def __init__(self, path: Path):
        self.path = path
        self.pending: dict[tuple[int, int], CleanupItem] = {}
        self.blocked_until: dict[int, float] = {}
        self.loaded = False

    def load(self) -> None:
        if self.loaded:
            return
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = {}
        self.pending = {
            (row["user_id"], row["message_id"]): CleanupItem(**row)
            for row in raw.get("pending", [])
        }
        self.blocked_until = {
            int(uid): float(deadline)
            for uid, deadline in raw.get("blocked_until", {}).items()
        }
        self.loaded = True

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "pending": [asdict(item) for item in self.pending.values()],
                "blocked_until": self.blocked_until,
            },
        )

    def remember(self, user_id: int, message_id: int) -> None:
        self.load()
        key = user_id, message_id
        if key not in self.pending:
            self.pending[key] = CleanupItem(*key)
            self.save()

    def forget(self, user_id: int, message_id: int) -> None:
        self.load()
        if self.pending.pop((user_id, message_id), None) is not None:
            self.save()

    def block(self, user_id: int, delay: float) -> None:
        self.load()
        self.blocked_until[user_id] = time.time() + delay
        self.save()

    def retry(self, user_id: int, message_id: int, delay: float) -> None:
        self.remember(user_id, message_id)
        item = self.pending[(user_id, message_id)]
        item.retry_at = time.time() + delay
        item.attempts += 1
        self.save()


cleanup_queue = KeyboardCleanup(config.config_dir / "keyboard-cleanup.json")
