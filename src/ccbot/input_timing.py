"""Content-free timing of one recoverable input across asynchronous stages."""

from __future__ import annotations

from contextlib import contextmanager
import logging
import time
from collections.abc import Generator
from typing import Any

logger = logging.getLogger(__name__)


def cause_type(exc: BaseException | None) -> str:
    seen: set[int] = set()
    while exc is not None and exc.__cause__ is not None and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.__cause__
    return type(exc).__name__ if exc is not None else "none"


@contextmanager
def input_stage(stage: str, request: Any = None) -> Generator[None, None, None]:
    from .input_recovery import input_recovery

    request = request or input_recovery.current()
    if request is None:
        yield
        return
    request.stage = stage
    input_recovery.changed(request)
    started = time.monotonic()
    outcome, cause = "ok", "none"
    logger.info(
        "input_stage message_id=%s request_id=%s session=%s kind=%s stage=%s event=start",
        request.message_id,
        request.id,
        request.session_id,
        request.kind,
        stage,
    )
    try:
        yield
    except BaseException as exc:
        outcome, cause = type(exc).__name__, cause_type(exc)
        raise
    finally:
        logger.info(
            "input_stage message_id=%s request_id=%s session=%s kind=%s stage=%s event=end outcome=%s cause_type=%s elapsed_ms=%s",
            request.message_id,
            request.id,
            request.session_id,
            request.kind,
            stage,
            outcome,
            cause,
            round((time.monotonic() - started) * 1000),
        )
