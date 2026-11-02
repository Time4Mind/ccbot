"""Recoverable media materialization before the at-most-once agent boundary."""

from __future__ import annotations

from pathlib import Path
from dataclasses import asdict
from typing import Any

from telegram.error import BadRequest, NetworkError, RetryAfter
from .input_timing import cause_type, input_stage
import logging

logger = logging.getLogger(__name__)


def encode_requests(records: dict[str, Any]) -> dict[str, Any]:
    """Keep the v1 request rows readable by the pre-deploy rollback commit."""
    rows, metadata = [], {}
    for request in records.values():
        row = asdict(request)
        metadata[request.id] = {
            key: row.pop(key) for key in ("media_path", "stage", "received_at")
        }
        rows.append(row)
    return {"requests": rows, "request_metadata": metadata}


def schedule_retry(recovery: Any, request: Any, exc: Exception) -> float:
    delay = recovery.retry_delay(request.attempts, exc)
    request.attempts += 1
    request.retry_at = recovery.clock() + delay
    recovery.changed(request)
    logger.info(
        "input_retry message_id=%s request_id=%s session=%s kind=%s stage=%s attempt=%s error_type=%s cause_type=%s wait_ms=%s",
        request.message_id,
        request.id,
        request.session_id,
        request.kind,
        request.stage,
        request.attempts,
        type(exc).__name__,
        cause_type(exc),
        round(delay * 1000),
    )
    return delay


async def download_voice(recovery: Any, voice: Any, request: Any) -> bytes:
    while True:
        if recovery.source(request) is None:
            raise ValueError(
                "Исходная сессия больше недоступна. Голосовое не перенаправлено."
            )
        if request.retry_at > recovery.clock():
            await recovery.sleep(request.retry_at - recovery.clock())
        request.state = "downloading"
        recovery.changed(request)
        try:
            with input_stage("get_file", request):
                file = await voice.get_file()
            with input_stage("download", request):
                audio = bytes(await file.download_as_bytearray())
        except (NetworkError, TimeoutError, RetryAfter) as exc:
            if isinstance(exc, BadRequest):
                raise
            schedule_retry(recovery, request, exc)
            continue
        request.state = "transcribing"
        request.retry_at = 0
        recovery.changed(request)
        return audio


def media_identity(filename: str) -> str:
    from .input_recovery import input_recovery

    request = input_recovery.current()
    return f"{request.id}:{filename}" if request is not None else ""


def retry_media_error(exc: Exception) -> None:
    """Let the durable worker retry reads; standalone callers keep error feedback."""
    from .input_recovery import input_recovery

    if (
        input_recovery.current() is not None
        and isinstance(exc, (NetworkError, TimeoutError, RetryAfter))
        and not isinstance(exc, BadRequest)
    ):
        raise exc


async def report_media_error(
    message: Any, exc: Exception, reply: Any, notice: str
) -> None:
    from .input_recovery import input_recovery

    retry_media_error(exc)
    request = input_recovery.current()
    if request is None:
        await reply(message, notice)
    else:
        request.notice = notice
        input_recovery.changed(request)


async def receive_file(
    sess: Any, workdir: str, filename: str, source: Any, save: Any
) -> str:
    from .input_recovery import input_recovery

    request = input_recovery.current()
    if request is not None and request.media_path:
        if (
            getattr(sess, "node_id", "local") != "local"
            or Path(request.media_path).is_file()
        ):
            return request.media_path

    async def fetch(target: Path) -> None:
        if request is not None:
            request.state = "downloading"
            input_recovery.changed(request)
        with input_stage("get_file"):
            file = await source.get_file()
        with input_stage("download"):
            await file.download_to_drive(target)

    path = await save(sess, workdir, filename, fetch)
    if request is not None:
        request.media_path = path
        request.state = "prepared"
        input_recovery.changed(request)
    return path
