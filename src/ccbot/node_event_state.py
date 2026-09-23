"""Durable leader cursor for idempotent worker event application."""

from __future__ import annotations

import json
import os
from pathlib import Path


class LeaderEventCursorStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path).expanduser() if path else None
        self._cursors: dict[str, int] = {}
        self._load()

    def last_applied(self, node_id: str) -> int:
        return self._cursors.get(node_id, 0)

    def commit(self, node_id: str, sequence: int) -> None:
        if not node_id or sequence < 1:
            return
        self._cursors[node_id] = max(sequence, self.last_applied(node_id))
        self._save()

    def _load(self) -> None:
        if self.path is None:
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(data, dict):
            return
        for node_id, sequence in data.items():
            try:
                parsed = int(sequence)
            except (TypeError, ValueError):
                continue
            if str(node_id) and parsed >= 0:
                self._cursors[str(node_id)] = parsed

    def _save(self) -> None:
        if self.path is None:
            return
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(self._cursors, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)


__all__ = ["LeaderEventCursorStore"]
