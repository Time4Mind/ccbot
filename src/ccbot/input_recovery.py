"""Durable recovery of voice admission and requests queued behind it.

The journal owns request identity and submission checkpoints, while the existing
per-session FIFO owns execution. An unacknowledged agent submission is never
replayed automatically. Telegram notices are retried separately from processing.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
import time
from typing import Any, Awaitable, Callable

from telegram import Update
from telegram.error import BadRequest, NetworkError, RetryAfter

from .config import config
from .final_delivery import RETRY_DELAYS, before_submission
from .utils import atomic_write_json

logger = logging.getLogger(__name__)
_current: ContextVar[str] = ContextVar("ccbot_recoverable_input", default="")


class SubmissionUnknown(Exception):
    """Input may already have reached its original agent."""


@dataclass
class InputRequest:
    id: str
    user_id: int
    message_id: int
    session_id: str
    window_id: str
    kind: str
    update: dict[str, Any]
    state: str = "accepted"
    confirmed: bool = False
    recognized: str = ""
    prepared: str = ""
    preprocessed: bool = False
    notice: str = ""
    notice_id: int | None = None
    notice_unknown: bool = False
    retry_at: float = 0.0
    attempts: int = 0
    notice_retry_at: float = 0.0
    notice_attempts: int = 0
    ui_retry_at: float = 0.0
    ui_attempts: int = 0
    ui_unknown: bool = False
    checkpoint: dict[str, Any] | None = None
    sequence: int = 0


class InputRecovery:
    """A private journal plus lifecycle-owned, coalescing feedback worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, InputRequest] = {}
        self.completed: list[str] = []
        self.loaded = False
        self.bot: Any = None
        self.task: asyncio.Task[None] | None = None
        self.events: dict[str, asyncio.Event] = {}
        self.dirty: set[tuple[int, str]] = set()
        self.clock: Callable[[], float] = time.time
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    def load(self) -> None:
        if self.loaded:
            return
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            raw = {}
        self.records = {
            row["id"]: InputRequest(**row) for row in raw.get("requests", [])
        }
        self.completed = raw.get("completed", [])
        for request in self.records.values():
            if request.state == "dispatching":
                request.state = "unknown"
                request.notice = "Передача запроса агенту не подтверждена. Автоповтор остановлен; проверьте исходную сессию и /requests."
            self.dirty.add((request.user_id, request.session_id))
        self.loaded = True
        if self.records:
            self.save()

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "version": 1,
                "requests": [asdict(r) for r in self.records.values()],
                "completed": self.completed,
            },
        )

    @staticmethod
    def key(update: Any) -> str:
        user = getattr(update, "effective_user", None)
        message = getattr(update, "message", None)
        return f"{getattr(user, 'id', 0)}:{getattr(message, 'message_id', 0)}"

    def duplicate(self, update: Any) -> bool:
        self.load()
        key = self.key(update)
        return key in self.records or key in self.completed

    def admit(self, update: Any, kind: str, session: Any) -> InputRequest | None:
        self.load()
        voice = getattr(update.message, "voice", None)
        file_id = getattr(voice, "file_id", None)
        follows_voice = any(
            r.session_id == session.id and r.state not in ("delivered", "failed")
            for r in self.records.values()
        )
        if not (
            kind == "voice" and isinstance(file_id, str) and file_id or follows_voice
        ):
            return None
        serialize = getattr(update, "to_dict", None)
        raw = serialize() if callable(serialize) else None
        if not isinstance(raw, dict):
            return None
        key = self.key(update)
        if key in self.completed:
            return None
        if key in self.records:
            return self.records[key]
        request = InputRequest(
            key,
            update.effective_user.id,
            update.message.message_id,
            session.id,
            session.window_id,
            kind,
            raw,
            sequence=max((r.sequence for r in self.records.values()), default=0) + 1,
        )
        self.records[key] = request
        self.changed(request)
        return request

    def current(self) -> InputRequest | None:
        return self.records.get(_current.get())

    def changed(self, request: InputRequest) -> None:
        self.dirty.add((request.user_id, request.session_id))
        self.save()

    def source(self, request: InputRequest) -> Any:
        from .session import session_manager

        session = session_manager.get_session(request.session_id)
        return (
            session
            if session is not None
            and session.state in ("active", "idle")
            and session.window_id == request.window_id
            else None
        )

    def complete(self, request: InputRequest, *, confirmed: bool = True) -> None:
        request.state = "delivered"
        request.confirmed = confirmed
        self.records.pop(request.id, None)
        self.completed.append(request.id)
        self.changed(request)
        if request.id in self.events:
            self.events[request.id].set()

    def fail(self, request: InputRequest, notice: str) -> None:
        from .handlers import bg_status

        bg_status.update_status(
            request.user_id, request.session_id, "error", force=True
        )
        request.state = "failed"
        request.notice = notice
        self.changed(request)
        if request.id in self.events:
            self.events[request.id].set()

    def ui_error(self, user_id: int, session_id: str, exc: BaseException) -> None:
        if (
            not isinstance(exc, (NetworkError, TimeoutError))
            or isinstance(exc, BadRequest)
            or before_submission(exc)
        ):
            return
        affected = [
            r
            for r in self.records.values()
            if r.user_id == user_id and r.session_id == session_id
        ]
        for item in affected:
            item.ui_unknown = True
        if affected:
            self.save()

    def retry_delay(self, attempts: int, exc: BaseException) -> float:
        if isinstance(exc, RetryAfter):
            wait = exc.retry_after
            return max(
                0.0,
                wait.total_seconds() if isinstance(wait, timedelta) else float(wait),
            )
        return RETRY_DELAYS[min(attempts, len(RETRY_DELAYS) - 1)]

    async def download(self, voice: Any, request: InputRequest) -> bytes:
        while True:
            if self.source(request) is None:
                raise ValueError(
                    "Исходная сессия больше недоступна. Голосовое не перенаправлено."
                )
            if request.retry_at > self.clock():
                await self.sleep(request.retry_at - self.clock())
            request.state = "downloading"
            self.changed(request)
            try:
                file = await voice.get_file()
                audio = bytes(await file.download_as_bytearray())
            except (NetworkError, TimeoutError, RetryAfter) as exc:
                if isinstance(exc, BadRequest):
                    raise
                request.retry_at = self.clock() + self.retry_delay(
                    request.attempts, exc
                )
                request.attempts += 1
                self.changed(request)
                continue
            request.state = "transcribing"
            request.retry_at = 0
            self.changed(request)
            return audio

    async def before_dispatch(self, wid: str, text: str) -> None:
        request = self.current()
        if request is None:
            return
        from .session import session_manager
        from .bot._messages_shared import _voice_transcript_checkpoint

        while session_manager.is_window_starting(wid):
            if self.source(request) is None:
                raise ValueError("Исходная сессия больше недоступна.")
            await self.sleep(0.5)
        if self.source(request) is None or wid != request.window_id:
            raise ValueError(
                "Исходная сессия больше недоступна; запрос не перенаправлен."
            )
        checkpoint = _voice_transcript_checkpoint(wid)
        request.checkpoint = (
            {
                "path": str(checkpoint.path),
                "offset": checkpoint.offset,
                "backend": checkpoint.backend,
            }
            if checkpoint is not None
            else None
        )
        request.prepared = text
        request.state = "dispatching"
        self.changed(request)

    async def submit(
        self, wid: str, text: str, session: Any, sender: Any
    ) -> tuple[bool, str]:
        await self.before_dispatch(wid, text)
        success, message = await sender(wid, text, session)
        self.after_dispatch(success, message)
        return success, message

    def after_dispatch(self, success: bool, message: str) -> None:
        request = self.current()
        if request is None:
            return
        if success and not message.startswith("Queued for "):
            self.complete(request)
            return
        request.state = "unknown"
        request.notice = "Передача запроса агенту не подтверждена. Автоповтор остановлен; проверьте исходную сессию и /requests."
        self.changed(request)
        raise SubmissionUnknown()

    def reconcile(self, request: InputRequest) -> bool:
        if request.checkpoint is None or not request.prepared:
            return False
        from .bot._messages_shared import (
            _VoiceTranscriptCheckpoint,
            _transcript_contains_voice_text,
        )

        checkpoint = _VoiceTranscriptCheckpoint(
            Path(request.checkpoint["path"]),
            request.checkpoint["offset"],
            request.checkpoint["backend"],
        )
        if _transcript_contains_voice_text(checkpoint, request.prepared):
            self.complete(request)
            return True
        return False

    async def run(
        self, request_id: str, processor: Any, update: Any, context: Any, wid: str
    ) -> bool:
        request = self.records.get(request_id)
        if request is None:
            return True
        token = _current.set(request_id)
        try:
            while request.id in self.records:
                if request.state == "failed":
                    return False
                if self.source(request) is None:
                    self.fail(
                        request,
                        "Исходная сессия больше недоступна. Запрос не перенаправлен в другую сессию.",
                    )
                    return False
                if request.state == "unknown":
                    if self.reconcile(request):
                        return True
                    event = self.events.setdefault(request.id, asyncio.Event())
                    try:
                        await asyncio.wait_for(event.wait(), timeout=1.0)
                    except TimeoutError:
                        pass
                    continue
                try:
                    if request.retry_at > self.clock():
                        await self.sleep(request.retry_at - self.clock())
                    result = await processor(update, context, wid)
                except asyncio.CancelledError:
                    if request.state == "dispatching":
                        try:
                            self.after_dispatch(False, "")
                        except SubmissionUnknown:
                            pass
                    raise
                except SubmissionUnknown:
                    continue
                except Exception as exc:
                    if request.id not in self.records:
                        return request.confirmed
                    if request.state == "dispatching":
                        try:
                            self.after_dispatch(False, "")
                        except SubmissionUnknown:
                            continue
                    if isinstance(
                        exc, (NetworkError, TimeoutError, RetryAfter)
                    ) and not isinstance(exc, BadRequest):
                        delay = self.retry_delay(request.attempts, exc)
                        request.attempts += 1
                        request.retry_at = self.clock() + delay
                        self.changed(request)
                        await self.sleep(delay)
                        continue
                    self.fail(
                        request,
                        request.notice
                        or "Не удалось обработать голосовое. Проверьте файл и исходную сессию.",
                    )
                    return False
                if request.id not in self.records:
                    return request.confirmed
                if result:
                    self.complete(request)
                    return True
                self.fail(
                    request,
                    request.notice
                    or "Запрос не был передан агенту. Проверьте исходную сессию и /requests.",
                )
                return False
            return request.confirmed
        finally:
            _current.reset(token)

    def start(self, application: Any) -> None:
        self.load()
        self.bot = application.bot
        from telegram.ext import CallbackContext
        from .bot import inbound
        from .inbound_queue import enqueue_inbound

        for request in sorted(self.records.values(), key=lambda r: r.sequence):
            if request.state == "failed":
                continue
            update = Update.de_json(request.update, self.bot)
            context = CallbackContext.from_update(update, application)
            processor = getattr(inbound, "_run_" + request.kind)
            enqueue_inbound(
                request.user_id,
                request.window_id,
                update,
                context,
                kind=request.kind,
                processor=processor,
                recovery_id=request.id,
            )
        self.task = asyncio.create_task(
            self.feedback_loop(), name="input-recovery-feedback"
        )

    async def deliver_notice(self, request: InputRequest) -> None:
        if (
            request.id not in self.records
            or self.bot is None
            or not request.notice
            or request.notice_unknown
            or request.notice_retry_at > self.clock()
        ):
            return
        from .telegram_rate_limit import background_telegram_request

        try:
            with background_telegram_request():
                if request.notice_id is not None:
                    await self.bot.edit_message_text(
                        chat_id=request.user_id,
                        message_id=request.notice_id,
                        text=request.notice,
                    )
                else:
                    request.notice_unknown = True
                    self.save()
                    message = await self.bot.send_message(
                        chat_id=request.user_id,
                        text=f"Голосовое / запрос №{request.message_id}: {request.notice}",
                        disable_notification=True,
                    )
                    request.notice_id = message.message_id
                    request.notice_unknown = False
                    self.save()
        except Exception as exc:
            already_rendered = (
                request.notice_id is not None
                and isinstance(exc, BadRequest)
                and "message is not modified" in str(exc).lower()
            )
            if not already_rendered:
                request.notice_unknown = request.notice_id is None and (
                    isinstance(exc, (NetworkError, TimeoutError))
                    and not isinstance(exc, BadRequest)
                    and not before_submission(exc)
                )
                request.notice_retry_at = self.clock() + self.retry_delay(
                    request.notice_attempts, exc
                )
                request.notice_attempts += 1
                self.changed(request)
                return
        if request.state == "failed":
            self.complete(request, confirmed=False)
        else:
            request.notice = ""
            self.save()

    async def feedback_loop(self) -> None:
        from .bot.input_feedback import refresh_request_status

        while True:
            try:
                for request in tuple(self.records.values()):
                    if request.state in ("failed", "unknown"):
                        if request.state == "unknown" and self.reconcile(request):
                            continue
                        await self.deliver_notice(request)
                for uid, sid in tuple(self.dirty):
                    if await refresh_request_status(self, uid, sid):
                        self.dirty.discard((uid, sid))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("input_recovery_feedback_failed")
            await self.sleep(1.0)

    async def shutdown(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        self.bot = None


input_recovery = InputRecovery(config.config_dir / "input-requests.json")


async def submit_input(
    wid: str, text: str, session: Any, sender: Any
) -> tuple[bool, str]:
    """Use the current journal instance while preserving the legacy send seam."""
    return await input_recovery.submit(wid, text, session, sender)
