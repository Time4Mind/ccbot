"""Read consistent ZCode session snapshots without writing its native database."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Generator

SESSION_ID = re.compile(r"^sess_[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_COLUMNS = {
    "session": {"id", "directory", "title", "time_updated", "parent_id"},
    "message": {"id", "session_id", "data", "time_updated", "time_created", "sequence"},
    "part": {
        "id",
        "message_id",
        "session_id",
        "data",
        "time_updated",
        "time_created",
        "sequence",
    },
}


class ZCodeStoreError(ValueError):
    """The installed store cannot safely satisfy this reader's contract."""


def database_path() -> Path:
    return Path(
        os.environ.get(
            "ZCODE_SESSION_DB_PATH", str(Path.home() / ".zcode/cli/db/db.sqlite")
        )
    ).expanduser()


@contextmanager
def open_store(path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    target = (path or database_path()).resolve()
    try:
        db = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=0.25)
    except sqlite3.Error as exc:
        raise ZCodeStoreError("ZCode session database is unavailable") from exc
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only = ON")
        db.execute("BEGIN")
        for table, wanted in _COLUMNS.items():
            columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
            if not wanted <= columns:
                raise ZCodeStoreError(f"Unsupported ZCode {table} schema")
        yield db
    except sqlite3.Error as exc:
        raise ZCodeStoreError("Cannot read ZCode session database") from exc
    finally:
        db.close()


def read_snapshot(
    session_id: str, cwd: str, *, path: Path | None = None, since: int = 0
) -> dict[str, Any] | None:
    if not SESSION_ID.fullmatch(session_id):
        raise ZCodeStoreError("Invalid ZCode session identity")
    with open_store(path) as db:
        row = db.execute("SELECT * FROM session WHERE id = ?", (session_id,)).fetchone()
        if row is None or (
            cwd and Path(row["directory"]).resolve() != Path(cwd).resolve()
        ):
            return None
        session = dict(row)
        if session.get("time_deleted") is not None:
            return None
        messages = db.execute(
            "SELECT * FROM message WHERE session_id = ? AND (time_updated >= ? OR id IN "
            "(SELECT message_id FROM part WHERE session_id = ? AND time_updated >= ?)) "
            "ORDER BY COALESCE(sequence, -1), time_created, rowid",
            (session_id, since, session_id, since),
        ).fetchall()
        result: list[dict[str, Any]] = []
        watermark = since
        for message in messages:
            parts = db.execute(
                "SELECT * FROM part WHERE session_id = ? AND message_id = ? "
                "ORDER BY COALESCE(sequence, -1), time_created, rowid",
                (session_id, message["id"]),
            ).fetchall()
            data = json.loads(message["data"])
            if not isinstance(data, dict):
                raise ZCodeStoreError("Invalid ZCode message record")
            data.update(id=message["id"], time_created=message["time_created"])
            data["parts"] = []
            watermark = max(watermark, message["time_updated"])
            for part in parts:
                item = json.loads(part["data"])
                if not isinstance(item, dict):
                    raise ZCodeStoreError("Invalid ZCode part record")
                item.update(id=part["id"])
                data["parts"].append(item)
                watermark = max(watermark, part["time_updated"])
            result.append(data)
        tables = {
            r["name"]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        usage = []
        if "model_usage" in tables:
            available = {
                r["name"] for r in db.execute("PRAGMA table_info(model_usage)")
            }
            allowed = (
                "id",
                "assistant_message_id",
                "started_at",
                "completed_at",
                "status",
                "provider_id",
                "model_id",
                "input_tokens",
                "output_tokens",
                "computed_total_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "reasoning_tokens",
                "error_code",
                "error_type",
            )
            if not {"id", "started_at", "status"} <= available:
                raise ZCodeStoreError("Unsupported ZCode usage schema")
            columns = ", ".join(name for name in allowed if name in available)
            usage = [
                dict(r)
                for r in db.execute(
                    f"SELECT {columns} FROM model_usage WHERE session_id = ? ORDER BY started_at, rowid",
                    (session_id,),
                )
            ]
        return {
            "session": session,
            "messages": result,
            "usage": usage,
            "watermark": watermark,
        }


def list_sessions(cwd: str, path: Path | None = None) -> list[dict[str, Any]]:
    with open_store(path) as db:
        rows = db.execute(
            "SELECT * FROM session WHERE directory = ? AND parent_id IS NULL ORDER BY time_updated DESC LIMIT 50",
            (str(Path(cwd).resolve()),),
        )
        return [
            dict(row)
            for row in rows
            if not dict(row).get("time_deleted") and SESSION_ID.fullmatch(row["id"])
        ]
