"""Recoverable media materialization before the at-most-once agent boundary."""

from __future__ import annotations

from pathlib import Path
from dataclasses import asdict
from contextlib import AsyncExitStack
from typing import Any

from telegram.error import BadRequest, NetworkError, RetryAfter
from .telegram_media import fresh_media_bot
from .input_timing import cause_type, input_stage
import logging

logger = logging.getLogger(__name__)


def encode_requests(records: dict[str, Any]) -> dict[str, Any]:
    """Keep the v1 request rows readable by the pre-deploy rollback commit."""
    rows, metadata, names, media = [], {}, {}, {}
    for request in records.values():
        row = asdict(request)
        names[request.id] = row.pop("session_name")
        media[request.id] = {
            key: row.pop(key) for key in ("media_failures", "media_deferred")
        }
        metadata[request.id] = {
            key: row.pop(key) for key in ("media_path", "stage", "received_at")
        }
        rows.append(row)
    return {
        "requests": rows,
        "request_metadata": metadata,
        "session_names": names,
        "media_recovery": media,
    }


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
    async with AsyncExitStack() as stack:
        fresh = None
        while request.media_failures < 5:
            if recovery.source(request) is None:
                raise ValueError(
                    "Исходная сессия больше недоступна. Голосовое не перенаправлено."
                )
            if request.retry_at > recovery.clock():
                await recovery.sleep(request.retry_at - recovery.clock())
            request.state = "downloading"
            recovery.changed(request)
            try:
                if request.media_failures >= 3 and fresh is None:
                    fresh = await stack.enter_async_context(fresh_media_bot(voice))
                    logger.info("input_media_new_client request_id=%s", request.id)
                with input_stage("get_file", request):
                    file = (
                        await fresh.get_file(voice.file_id)
                        if fresh is not None
                        else await voice.get_file()
                    )
                with input_stage("download", request):
                    audio = bytes(await file.download_as_bytearray())
            except (NetworkError, TimeoutError, RetryAfter) as exc:
                if isinstance(exc, BadRequest):
                    raise
                if not isinstance(exc, RetryAfter):
                    request.media_failures += 1
                if request.media_failures >= 5:
                    break
                schedule_retry(recovery, request, exc)
                continue
            request.state = "transcribing"
            request.retry_at = 0
            recovery.changed(request)
            return audio
    request.media_deferred = True
    request.retry_at = 0
    recovery.fail(
        request,
        "Не удалось получить голосовое после 5 сетевых сбоев, включая новый клиент. "
        "Голосовое сохранено; следующие сообщения продолжат обработку. "
        f"Повторить: /requests retry {request.message_id}",
    )
    raise ValueError(request.notice)


def with_media_gap(recovery: Any, text: str) -> str:
    current = recovery.current()
    if current is None or text.lstrip().startswith("/"):
        return text
    missing = [
        str(r.message_id)
        for r in recovery.records.values()
        if r.session_id == current.session_id
        and r.media_deferred
        and r.state == "failed"
        and r.sequence < current.sequence
    ]
    if not missing:
        return text
    return (
        "[Сообщение ccbot: предыдущее голосовое не получено агентом "
        "из-за ошибки загрузки, запросы №"
        + ", №".join(missing)
        + ". Не восстанавливай его содержание по догадке.]\n\n"
        + text
    )


def retry_deferred_media(recovery: Any, request: Any, application: Any) -> bool:
    """Only retry known, never-submitted media into its original source."""
    from telegram import Update
    from telegram.ext import CallbackContext
    from .bot import inbound
    from .inbound_queue import enqueue_inbound

    if (
        not request.media_deferred
        or request.state != "failed"
        or recovery.source(request) is None
    ):
        return False
    request.media_failures = 0
    request.media_deferred = False
    request.attempts = 0
    request.retry_at = 0
    request.notice = ""
    request.notice_id = None
    request.notice_unknown = False
    request.notice_retry_at = 0
    request.notice_attempts = 0
    request.state = "accepted"
    recovery.changed(request)
    update = Update.de_json(request.update, application.bot)
    enqueue_inbound(
        request.user_id,
        request.window_id,
        update,
        CallbackContext.from_update(update, application),
        kind="voice",
        processor=getattr(inbound, "_run_voice"),
        recovery_id=request.id,
    )
    return True


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
