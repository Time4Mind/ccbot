"""Read-only snapshots of OpenCode's native SQLite message store."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Generator

SESSION_ID = re.compile(r"^ses_[A-Za-z0-9]{1,128}$")
_COLUMNS = {
    "session": {"id", "directory", "title", "parent_id", "time_updated"},
    "message": {"id", "session_id", "data", "time_created", "time_updated"},
    "part": {"id", "message_id", "session_id", "data", "time_created", "time_updated"},
}


class OpenCodeStoreError(ValueError):
    """The native store cannot safely satisfy the reader contract."""


def database_path() -> Path:
    explicit = os.environ.get("OPENCODE_SESSION_DB_PATH")
    if explicit:
        return Path(explicit).expanduser()
    data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    return data / "opencode/opencode.db"


@contextmanager
def open_store(path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    target = (path or database_path()).resolve()
    try:
        db = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error as exc:
        raise OpenCodeStoreError("OpenCode session database is unavailable") from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        for table, required in _COLUMNS.items():
            available = {
                row["name"] for row in db.execute(f"PRAGMA table_info({table})")
            }
            if not required <= available:
                raise OpenCodeStoreError(f"Unsupported OpenCode {table} schema")
        yield db
    except sqlite3.Error as exc:
        raise OpenCodeStoreError("Cannot read OpenCode session database") from exc
    finally:
        db.close()


def read_snapshot(
    session_id: str, cwd: str, *, path: Path | None = None, since: int = 0
) -> dict[str, Any] | None:
    if not SESSION_ID.fullmatch(session_id):
        raise OpenCodeStoreError("Invalid OpenCode session identity")
    with open_store(path) as db:
        row = db.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        if row is None or (
            cwd and Path(row["directory"]).resolve() != Path(cwd).resolve()
        ):
            return None
        messages = db.execute(
            "SELECT * FROM message WHERE session_id=? AND (time_updated>=? OR id IN "
            "(SELECT message_id FROM part WHERE session_id=? AND time_updated>=?)) "
            "ORDER BY time_created, rowid",
            (session_id, since, session_id, since),
        ).fetchall()
        result, usage = [], []
        watermark = since
        for message in messages:
            data = json.loads(message["data"])
            if not isinstance(data, dict):
                raise OpenCodeStoreError("Invalid OpenCode message")
            data.update(id=message["id"], time_created=message["time_created"])
            data["modelId"] = data.get("modelID", "")
            data["parts"] = []
            watermark = max(watermark, message["time_updated"])
            for part in db.execute(
                "SELECT * FROM part WHERE session_id=? AND message_id=? ORDER BY time_created, rowid",
                (session_id, message["id"]),
            ):
                item = json.loads(part["data"])
                if not isinstance(item, dict):
                    raise OpenCodeStoreError("Invalid OpenCode part")
                item["id"] = part["id"]
                data["parts"].append(item)
                watermark = max(watermark, part["time_updated"])
            if data.get("role") == "assistant" and (data.get("time") or {}).get(
                "completed"
            ):
                tokens = data.get("tokens") or {}
                cache = tokens.get("cache") or {}
                incoming = int(tokens.get("input") or 0)
                outgoing = int(tokens.get("output") or 0)
                cached = int(cache.get("read") or 0)
                written = int(cache.get("write") or 0)
                usage.append(
                    {
                        "id": message["id"],
                        "assistant_message_id": message["id"],
                        "started_at": message["time_created"],
                        "status": "completed",
                        "model_id": data["modelId"],
                        "input_tokens": incoming,
                        "output_tokens": outgoing,
                        "cache_read_input_tokens": cached,
                        "cache_creation_input_tokens": written,
                        "computed_total_tokens": incoming + outgoing + cached + written,
                    }
                )
            result.append(data)
        return {
            "session": dict(row),
            "messages": result,
            "usage": usage,
            "watermark": watermark,
        }


def list_sessions(cwd: str, path: Path | None = None) -> list[dict[str, Any]]:
    with open_store(path) as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM session WHERE directory=? AND parent_id IS NULL "
                "ORDER BY time_updated DESC LIMIT 50",
                (str(Path(cwd).resolve()),),
            )
            if SESSION_ID.fullmatch(row["id"])
        ]


def created_after(session_id: str, timestamp_ms: float) -> bool:
    """Distinguish newly born live sessions from restored native history."""
    try:
        with open_store() as db:
            row = db.execute(
                "SELECT time_created FROM session WHERE id=?", (session_id,)
            ).fetchone()
        return row is not None and row["time_created"] >= timestamp_ms
    except OpenCodeStoreError:
        return False
