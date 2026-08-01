"""Locked read-modify-write helpers for the hook-owned session map."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any

from .utils import atomic_write_json


def upsert_session_map_entry(
    map_file: Path,
    key: str,
    entry: dict[str, str],
) -> dict[str, str]:
    """Merge one canonical row without losing concurrent hook updates."""
    map_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = map_file.with_suffix(".lock")
    with lock_path.open("a+") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            session_map: dict[str, Any] = {}
            if map_file.exists():
                try:
                    loaded = json.loads(map_file.read_text())
                except (json.JSONDecodeError, OSError) as exc:
                    raise RuntimeError(f"cannot read {map_file}") from exc
                if not isinstance(loaded, dict):
                    raise RuntimeError(f"invalid session map root in {map_file}")
                session_map = loaded
            existing = session_map.get(key)
            merged = dict(existing) if isinstance(existing, dict) else {}
            merged.update(entry)
            session_map[key] = merged
            atomic_write_json(map_file, session_map)
            return merged
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
