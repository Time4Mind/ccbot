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


def remove_session_map_entries(map_file: Path, session_id: str) -> list[str]:
    """Remove every window row for one exact provider session id."""
    if not session_id:
        return []
    map_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = map_file.with_suffix(".lock")
    with lock_path.open("a+") as lock_handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_handle, fcntl.LOCK_EX)
        try:
            if not map_file.exists():
                return []
            try:
                loaded = json.loads(map_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                raise RuntimeError(f"cannot read {map_file}") from exc
            if not isinstance(loaded, dict):
                raise RuntimeError(f"invalid session map root in {map_file}")
            removed = sorted(
                key
                for key, value in loaded.items()
                if isinstance(value, dict) and value.get("session_id") == session_id
            )
            if not removed:
                return []
            for key in removed:
                del loaded[key]
            atomic_write_json(map_file, loaded)
            return removed
        finally:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)


def reconcile_opencode_entry(
    map_file: Path, key: str, observed: dict[str, str]
) -> dict[str, str] | None:
    """Heal a child row left by an older hook without clobbering a newer bind."""
    from .opencode_store import root_session_id
    from .opencode_session_io import ensure_projection

    sid, cwd = observed.get("session_id"), observed.get("cwd")
    if not sid or not cwd:
        return None
    root = root_session_id(sid, cwd)
    if root is None:
        return None
    if root == sid:
        return observed

    with map_file.with_suffix(".lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(map_file.read_text())
        if current.get(key) != observed:
            return None
        if any(
            other.rsplit(":", 1)[-1] != key.rsplit(":", 1)[-1]
            and isinstance(row, dict)
            and row.get("backend") == "opencode"
            and row.get("session_id") == root
            for other, row in current.items()
        ):
            return None
        repaired = {
            **observed,
            "session_id": root,
            "transcript_path": str(ensure_projection(root, cwd)),
        }
        current[key] = repaired
        atomic_write_json(map_file, current)
        return repaired
