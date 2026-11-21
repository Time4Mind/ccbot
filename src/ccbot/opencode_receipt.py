"""Verify prompt delivery against user messages in the original OpenCode database."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, TYPE_CHECKING

from .opencode_store import database_path, open_store, OpenCodeStoreError
from .utils import ccbot_dir


if TYPE_CHECKING:
    from .input_recovery import InputRequest


def _binding(window: str) -> dict[str, Any] | None:
    try:
        bindings = json.loads((ccbot_dir() / "session_map.json").read_text())
        matches = [
            v
            for k, v in bindings.items()
            if k.endswith(":" + window)
            and isinstance(v, dict)
            and v.get("backend") == "opencode"
        ]
        return matches[0] if len(matches) == 1 else None
    except (OSError, ValueError):
        return None


def native_checkpoint(window: str) -> dict[str, Any] | None:
    binding = _binding(window)
    if not binding:
        return None
    path = database_path().resolve()
    try:
        with open_store(path) as db:
            row = db.execute(
                "SELECT id, directory FROM session WHERE id=? AND directory=?",
                (binding["session_id"], binding["cwd"]),
            ).fetchone()
            if row is None:
                return None
            last = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()[
                0
            ]
            return {
                "session_id": row["id"],
                "cwd": row["directory"],
                "db": str(path),
                "rowid": last,
            }
    except (KeyError, OSError, OpenCodeStoreError):
        return None


def received(checkpoint: dict[str, Any], payload: str, *, photo: bool) -> bool:
    expected = payload.replace("\r\n", "\n").strip()
    try:
        with open_store(Path(checkpoint["db"])) as db:
            session = db.execute(
                "SELECT directory FROM session WHERE id=?", (checkpoint["session_id"],)
            ).fetchone()
            if (
                not session
                or Path(session["directory"]).resolve()
                != Path(checkpoint["cwd"]).resolve()
            ):
                return False
            rows = db.execute(
                "SELECT id FROM message WHERE session_id=? AND rowid>? AND time_created>=? "
                "AND json_extract(data,'$.role')='user'",
                (
                    checkpoint["session_id"],
                    checkpoint.get("rowid", 0),
                    checkpoint.get("since", 0),
                ),
            )
            for row in rows:
                parts = db.execute(
                    "SELECT json_extract(data,'$.type') AS kind, json_extract(data,'$.text') AS text, "
                    "json_extract(data,'$.mime') AS mime, json_extract(data,'$.source.path') AS path "
                    "FROM part WHERE session_id=? AND message_id=? "
                    "AND NOT coalesce(json_extract(data,'$.synthetic'),0) ORDER BY time_created,rowid",
                    (checkpoint["session_id"], row["id"]),
                ).fetchall()
                text = (
                    "\n".join(p["text"] or "" for p in parts if p["kind"] == "text")
                    .replace("\r\n", "\n")
                    .strip()
                )
                files = [p for p in parts if p["kind"] == "file"]
                if not files:
                    if not photo and text == expected:
                        return True
                    continue
                if len(files) != 1 or not (files[0]["mime"] or "").startswith("image/"):
                    continue
                path = files[0]["path"]
                if not isinstance(path, str) or not path.startswith(".ccbot-inbox/"):
                    continue
                marker = re.search(r"\[Image [1-9][0-9]*\]$", text)
                if marker and text[: marker.start()] + path == expected:
                    return True
    except (KeyError, OSError, ValueError):
        pass
    return False


def recovered_receipt(request: InputRequest) -> bool:
    checkpoint = request.checkpoint or {}
    native = checkpoint.get("native")
    if native is None:
        # Pre-upgrade journals identify the original store and session in their header.
        if not checkpoint.get("path") or request.dispatch_started_at <= 0:
            return False
        from .opencode_session_io import projection_header

        header = projection_header(Path(checkpoint["path"]))
        if not all(header.get(k) for k in ("database_path", "sessionId", "cwd")):
            return False
        native = {
            "db": header["database_path"],
            "session_id": header["sessionId"],
            "cwd": header["cwd"],
            "since": int(request.dispatch_started_at * 1000) + 1,
        }
    return received(native, request.prepared, photo=request.kind == "photo")
