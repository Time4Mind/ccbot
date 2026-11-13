"""Serialize OpenCode prompts until its TUI and native journal accept them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import time
from weakref import WeakValueDictionary

from .opencode_runtime import is_ready
from .opencode_store import OpenCodeStoreError, open_store
from .utils import ccbot_dir

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
READY_TIMEOUT = 120.0
ACK_TIMEOUT = 10.0


async def _capture(window: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "tmux",
        "capture-pane",
        "-p",
        "-t",
        window,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    output, _ = await proc.communicate()
    return output.decode(errors="replace") if proc.returncode == 0 else ""


def _accepted(window: str) -> dict[tuple[str, str], str]:
    try:
        bindings = json.loads((ccbot_dir() / "session_map.json").read_text())
        matches = [
            v
            for k, v in bindings.items()
            if k.endswith(":" + window) and v.get("backend") == "opencode"
        ]
        if len(matches) != 1:
            return {}
        binding = matches[0]
        with open_store() as db:
            session = db.execute(
                "SELECT directory FROM session WHERE id=?", (binding["session_id"],)
            ).fetchone()
            if session is None or session["directory"] != binding["cwd"]:
                return {}
            result = {}
            for row in db.execute(
                "SELECT id,data FROM message WHERE session_id=? ORDER BY time_created DESC,rowid DESC LIMIT 40",
                (binding["session_id"],),
            ):
                if json.loads(row["data"]).get("role") != "user":
                    continue
                parts = [
                    json.loads(p["data"])
                    for p in db.execute(
                        "SELECT data FROM part WHERE message_id=? ORDER BY time_created,rowid",
                        (row["id"],),
                    )
                ]
                result[(binding["session_id"], row["id"])] = "\n".join(
                    p.get("text", "")
                    for p in parts
                    if p.get("type") == "text" and not p.get("synthetic")
                )
            return result
    except (OSError, ValueError, KeyError, OpenCodeStoreError):
        return {}


async def submit(window: str, text: str, send: Callable[[], Awaitable[bool]]) -> bool:
    """Wait without holding navigation locks; never retype uncertain input."""
    from .tmux_input_transport import zcode_process_is_running

    lock = _locks.setdefault(window, asyncio.Lock())
    async with lock:
        deadline = time.monotonic() + READY_TIMEOUT
        while not is_ready(await _capture(window)):
            if time.monotonic() >= deadline or not await zcode_process_is_running(
                window, backend="opencode"
            ):
                return False
            await asyncio.sleep(0.1)
        before = await asyncio.to_thread(_accepted, window)
        if not await send():
            return False
        if text.lstrip().startswith("/"):
            return True
        deadline = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            accepted = await asyncio.to_thread(_accepted, window)
            if any(
                key not in before
                and value.replace("\r\n", "\n").strip() == text.strip()
                for key, value in accepted.items()
            ):
                return True
            if not await zcode_process_is_running(window, backend="opencode"):
                return False
            await asyncio.sleep(0.1)
        return False
