"""Serialize OpenCode prompts until its TUI and native journal accept them."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
import time
from weakref import WeakValueDictionary
from typing import TYPE_CHECKING

from .opencode_runtime import composer_is_available, is_ready
from .opencode_receipt import bind_pending_checkpoint, native_checkpoint, received

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
        before = (
            await asyncio.to_thread(
                native_checkpoint, window, request.session_id if request else ""
            )
            if not command
            else None
        )
        if request is not None and (
            input_recovery.current() is not request
            or input_recovery.source(request) is None
        ):
            return False
        hook = _before_paste.get()
        if hook is not None and hook[0] == window:
            hook[1]()
        if request is not None and before is not None:
            if request.checkpoint is None:
                request.checkpoint = {"backend": "opencode", "path": "", "offset": 0}
            request.checkpoint["native"] = before
        _submission_phase(window, waiting=False)
        if not await send():
            return False
        if command:
            return True
        deadline = time.monotonic() + ACK_TIMEOUT
        while time.monotonic() < deadline:
            if before and "session_id" not in before:
                if bind_pending_checkpoint(before) and request is not None:
                    input_recovery.save()
            if before is not None and await asyncio.to_thread(
                received,
                before,
                text,
                photo=request is not None and request.kind == "photo",
            ):
                return True
            if not await zcode_process_is_running(window, backend="opencode"):
                return False
            await asyncio.sleep(0.1)
        return False
