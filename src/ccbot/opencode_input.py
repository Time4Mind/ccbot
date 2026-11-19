"""Serialize OpenCode prompts until its TUI and native journal accept them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
from weakref import WeakValueDictionary
from typing import TYPE_CHECKING, Any

from .opencode_runtime import composer_is_available, is_ready
from .opencode_store import OpenCodeStoreError, open_store
from .utils import ccbot_dir

if TYPE_CHECKING:
    from .input_recovery import InputRequest

_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
_before_paste: ContextVar[tuple[str, Callable[[], None]] | None] = ContextVar(
    "opencode_before_paste", default=None
)
READY_TIMEOUT = 120.0
ACK_TIMEOUT = 10.0


@contextmanager
def on_prompt_paste(window: str, callback: Callable[[], None]):
    token = _before_paste.set((window, callback))
    try:
        yield
    finally:
        _before_paste.reset(token)


def _submission_phase(window: str, *, waiting: bool) -> InputRequest | None:
    from .input_recovery import input_recovery

    request = input_recovery.current()
    if request is not None and request.window_id == window:
        # A restart may replay an untouched FIFO entry. Once paste begins,
        # an uncertain outcome must retain the normal no-autoretry policy.
        request.state = "prepared" if waiting else "dispatching"
        input_recovery.changed(request)
        return request
    return None


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


def _accepted(window: str) -> dict[tuple[str, str], str]:
    try:
        binding = _binding(window)
        if binding is None:
            return {}
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
    from .input_recovery import input_recovery
    from .tmux_input_transport import zcode_process_is_running

    request = _submission_phase(window, waiting=True)
    command = text.lstrip().startswith("/")
    # OpenCode accepts normal follow-ups while its current run is working.
    input_ready = is_ready if command else composer_is_available
    # Only the local inbound FIFO owns a durable request it can safely keep.
    deadline = (
        None
        if request is not None and not command
        else time.monotonic() + READY_TIMEOUT
    )
    lock = _locks.setdefault(window, asyncio.Lock())
    async with lock:
        while True:
            if request is not None and (
                input_recovery.current() is not request
                or input_recovery.source(request) is None
            ):
                return False
            if input_ready(await _capture(window)):
                break
            if (deadline is not None and time.monotonic() >= deadline) or (
                not await zcode_process_is_running(window, backend="opencode")
            ):
                return False
            await asyncio.sleep(0.25)
        before = await asyncio.to_thread(_accepted, window)
        if request is not None and (
            input_recovery.current() is not request
            or input_recovery.source(request) is None
        ):
            return False
        hook = _before_paste.get()
        if hook is not None and hook[0] == window:
            hook[1]()
        _submission_phase(window, waiting=False)
        if not await send():
            return False
        if command:
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
