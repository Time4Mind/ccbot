"""Verify prompt delivery against user messages in the original OpenCode database."""

from __future__ import annotations

import json
from pathlib import Path
import re
import time
from typing import Any, TYPE_CHECKING

from .opencode_store import database_path, open_store, OpenCodeStoreError
from .session_keys import key_matches_window
from .utils import ccbot_dir


if TYPE_CHECKING:
    from .input_recovery import InputRequest


def window_binding(window: str) -> dict[str, Any] | None:
    try:
        from .session_map_store import reconcile_opencode_entry

        path = ccbot_dir() / "session_map.json"
        bindings = json.loads(path.read_text())
        matches = [
            (k, v)
            for k, v in bindings.items()
            if key_matches_window(k, window)
            and isinstance(v, dict)
            and v.get("backend") == "opencode"
        ]
        if len(matches) != 1:
            return None
        key, entry = matches[0]
        return reconcile_opencode_entry(path, key, entry)
    except (KeyError, OSError, ValueError):
        return None


def native_checkpoint(window: str, owner_id: str = "") -> dict[str, Any] | None:
    from .session import session_manager

    binding = window_binding(window)
    path = database_path().resolve()
    try:
        with open_store(path) as db:
            last = db.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()[
                0
            ]
            checkpoint = {"db": str(path), "rowid": last}
            if binding and binding.get("session_id"):
                row = db.execute(
                    "SELECT id,directory FROM session WHERE id=? AND directory=?",
                    (binding["session_id"], binding["cwd"]),
                ).fetchone()
                if row is not None:
                    return {
                        **checkpoint,
                        "session_id": row["id"],
                        "cwd": row["directory"],
                    }
                return None
            owner = session_manager.find_session_by_window(window)
            if (
                owner is None
                or owner.backend != "opencode"
                or owner.node_id != "local"
                or not owner.workdir
                or (owner_id and owner.id != owner_id)
            ):
                return None
            return {
                **checkpoint,
                "window": window,
                "owner": owner.id,
                "activated_at": owner.activated_at,
                "cwd": owner.workdir,
                "since": int(time.time() * 1000),
            }
    except (KeyError, OSError, OpenCodeStoreError):
        return None


def bind_pending_checkpoint(checkpoint: dict[str, Any]) -> bool:
    from .session import session_manager

    if checkpoint.get("session_id") or not checkpoint.get("window"):
        return False
    window = checkpoint["window"]
    owner = session_manager.get_session(checkpoint["owner"])
    if (
        owner is None
        or owner.state not in ("idle", "active")
        or owner.window_id != window
        or owner.backend != "opencode"
        or owner.node_id != "local"
        or owner.activated_at != checkpoint["activated_at"]
    ):
        return False
    binding = window_binding(window)
    if (
        not binding
        or not binding.get("session_id")
        or not binding.get("cwd")
        or Path(binding["cwd"]).resolve() != Path(checkpoint["cwd"]).resolve()
        or owner.claude_session_id
        and owner.claude_session_id != binding["session_id"]
    ):
        return False
    checkpoint["session_id"] = binding["session_id"]
    return True


def normalize_prompt(text: str) -> str:
    """OpenCode paste can change line endings and add outer whitespace."""
    return text.replace("\r\n", "\n").strip()


def received(checkpoint: dict[str, Any], payload: str, *, photo: bool) -> bool:
    expected = normalize_prompt(payload)
    try:
        session_id = checkpoint.get("session_id")
        if not session_id:
            return False
        with open_store(Path(checkpoint["db"])) as db:
            session = db.execute(
                "SELECT directory FROM session WHERE id=?", (session_id,)
            ).fetchone()
            if (
                not session
                or Path(session["directory"]).resolve()
                != Path(checkpoint["cwd"]).resolve()
            ):
                return False
            query = (
                "SELECT id FROM message WHERE session_id=? AND rowid>? AND time_created>=? "
                "AND json_extract(data,'$.role')='user'"
            )
            params = [
                session_id,
                checkpoint.get("rowid", 0),
                checkpoint.get("since", 0),
            ]
            if "message_id" in checkpoint:
                query += " AND id=?"
                params.append(checkpoint["message_id"])
            for row in db.execute(query, params):
                parts = db.execute(
                    "SELECT json_extract(data,'$.type') AS kind, json_extract(data,'$.text') AS text, "
                    "json_extract(data,'$.mime') AS mime, json_extract(data,'$.source.path') AS path "
                    "FROM part WHERE session_id=? AND message_id=? "
                    "AND NOT coalesce(json_extract(data,'$.synthetic'),0) ORDER BY time_created,rowid",
                    (session_id, row["id"]),
                ).fetchall()
                text = normalize_prompt(
                    "\n".join(p["text"] or "" for p in parts if p["kind"] == "text")
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


def legacy_first_checkpoint(request: InputRequest) -> dict[str, Any] | None:
    """Recover an old journal only from a newly created, still-bound session's first turn."""
    from .session import session_manager
    from .opencode_input import ACK_TIMEOUT

    owner = session_manager.get_session(request.session_id)
    if (
        request.state != "unknown"
        or owner is None
        or owner.backend != "opencode"
        or not owner.claude_session_id
        or not owner.workdir
        or not 0 < owner.activated_at <= request.dispatch_started_at
    ):
        return None
    since = int(request.dispatch_started_at * 1000) + 1
    end = since + int(ACK_TIMEOUT * 1000)
    path = database_path().resolve()
    native: dict[str, Any] = {
        "db": str(path),
        "cwd": owner.workdir,
        "window": request.window_id,
        "owner": owner.id,
        "activated_at": owner.activated_at,
        "since": since,
    }
    if not bind_pending_checkpoint(native):
        return None
    try:
        with open_store(path) as db:
            row = db.execute(
                "SELECT m.id, m.time_created FROM message m JOIN session s ON s.id=m.session_id "
                "WHERE s.id=? AND s.time_created BETWEEN ? AND ? AND json_extract(m.data,'$.role')='user' "
                "ORDER BY m.time_created,m.rowid LIMIT 1",
                (native["session_id"], since, end),
            ).fetchone()
            if row is not None and since <= row["time_created"] <= end:
                return {**native, "message_id": row["id"]}
    except (OpenCodeStoreError, OSError):
        pass
    return None


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
