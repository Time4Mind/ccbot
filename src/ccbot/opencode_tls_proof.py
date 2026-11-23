"""Read-only evidence for replaying one text-only OpenCode provider attempt."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from .opencode_store import open_store

_TLS = re.compile(
    r"EPROTO:\s*The TLS handshake failed\.?(?: Does the server speak TLS on this port\?)?",
    re.I,
)


def handshake_error(error: Any) -> bool:
    if not isinstance(error, dict) or error.get("name") != "UnknownError":
        return False
    data = error.get("data")
    return (
        isinstance(data, dict)
        and not any(
            value is not None for key, value in data.items() if key != "message"
        )
        and bool(_TLS.fullmatch(str(data.get("message", "")).strip()))
    )


def digest(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode()).hexdigest()


@dataclass(frozen=True)
class Attempt:
    user: str
    text: str
    error_row: int = 0


def inspect_attempt(
    path: str, sid: str, cwd: str, floor: int, expected: str, error_id: str = ""
) -> Attempt | None:
    """Require one fresh native user row and, for replay, its exact failed reply."""
    with open_store(Path(path)) as db:
        if not db.execute(
            "SELECT 1 FROM session WHERE id=? AND directory=? AND parent_id IS NULL",
            (sid, cwd),
        ).fetchone():
            return None
        rows = db.execute(
            "SELECT rowid,id,data FROM message WHERE session_id=? AND rowid>? ORDER BY rowid LIMIT 3",
            (sid, floor),
        ).fetchall()
        if not rows or json.loads(rows[0]["data"]).get("role") != "user":
            return None
        user = rows[0]["id"]

        def parts(mid: str):
            return [
                json.loads(p[0])
                for p in db.execute(
                    "SELECT data FROM part WHERE session_id=? AND message_id=? ORDER BY rowid",
                    (sid, mid),
                )
            ]

        prompts = parts(user)
        if (
            len(prompts) != 1
            or prompts[0].get("type") != "text"
            or prompts[0].get("synthetic")
            or prompts[0].get("ignored")
        ):
            return None
        text = prompts[0].get("text")
        if (
            not isinstance(text, str)
            or not text.strip()
            or text.lstrip().startswith("/")
            or digest(text) != expected
        ):
            return None
        if not error_id:
            return Attempt(user, text)
        if len(rows) != 2 or rows[1]["id"] != error_id:
            return None
        reply = json.loads(rows[1]["data"])
        if (
            reply.get("role") != "assistant"
            or reply.get("parentID") != user
            or (reply.get("tokens") or {}).get("output", 0)
            or (reply.get("tokens") or {}).get("reasoning", 0)
            or not handshake_error(reply.get("error"))
        ):
            return None
        if any(
            p.get("type") not in ("step-start", "step-finish") for p in parts(error_id)
        ):
            return None
        return Attempt(user, text, rows[1]["rowid"])
