"""Durable keyboard cleanup and ownership of retained final answers."""

from __future__ import annotations
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import time
from .config import config
from .utils import atomic_write_json

_FINAL_SESSION_LIMIT = 2000


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
        self.session_links: dict[tuple[int, int], str] = {}
        self.final_sessions: dict[tuple[int, int], str | None] = {}
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
        self.session_links = {
            (row["user_id"], row["message_id"]): row["session_id"]
            for row in raw.get("session_links", [])
            if (row["user_id"], row["message_id"]) in self.pending
        }
        self.final_sessions = {
            (row["user_id"], row["message_id"]): row["session_id"]
            for row in raw.get("final_sessions", [])[-_FINAL_SESSION_LIMIT:]
        }
        self.loaded = True

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "pending": [asdict(item) for item in self.pending.values()],
                "blocked_until": self.blocked_until,
                # Keep pending rows readable by the previous release on rollback.
                "final_sessions": [
                    {"user_id": uid, "message_id": mid, "session_id": sid}
                    for (uid, mid), sid in self.final_sessions.items()
                ],
                "session_links": [
                    {"user_id": uid, "message_id": mid, "session_id": sid}
                    for (uid, mid), sid in self.session_links.items()
                ],
            },
        )

    def remember_final(
        self,
        user_id: int,
        message_id: int,
        session_id: str,
        *,
        only_if_unknown: bool = False,
    ) -> None:
        """Keep ownership after delivery/cleanup, including across restarts."""
        self.load()
        key = user_id, message_id
        if self.final_sessions.get(key) == session_id or (
            only_if_unknown and key in self.final_sessions
        ):
            return
        self.final_sessions.pop(key, None)
        self.final_sessions[key] = session_id
        if len(self.final_sessions) > _FINAL_SESSION_LIMIT:
            self.final_sessions.pop(next(iter(self.final_sessions)))
        self.save()

    def rebind_final(
        self, user_id: int, message_id: int, session_id: str | None
    ) -> None:
        """Forget old content ownership only after another session repaints it."""
        self.load()
        key = user_id, message_id
        owner = self.final_sessions.get(key)
        if owner is not None and session_id is not None and owner != session_id:
            # Retain an invalidation receipt: delayed delivery recovery must
            # not revive the previous owner's link after a successful repaint.
            self.final_sessions[key] = None
            self.save()

    def remember(
        self, user_id: int, message_id: int, *, session_id: str | None = None
    ) -> None:
        self.load()
        key = user_id, message_id
        changed = key not in self.pending
        if changed:
            self.pending[key] = CleanupItem(*key)
        if session_id is not None and self.session_links.get(key) != session_id:
            self.session_links[key] = session_id
            changed = True
        if changed:
            self.save()

    def forget(self, user_id: int, message_id: int) -> None:
        self.load()
        key = user_id, message_id
        self.session_links.pop(key, None)
        if self.pending.pop(key, None) is not None:
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
