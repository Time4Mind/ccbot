"""Durable recovery of voice admission and requests queued behind it.

The journal owns request identity and submission checkpoints, while the existing
per-session FIFO owns execution. An unacknowledged agent submission is never
replayed automatically. Telegram notices are retried separately from processing.
"""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import datetime
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
_CONFIRMATION_GRACE_SECONDS = 30.0
_CONFIRMATION_CHECK_SECONDS = 5.0


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
    confirmation_deadline: float = 0.0
    strict_codex_proof: bool = False
    submitted_at: float = 0.0
    dispatch_started_at: float = 0.0


class InputRecovery:
    """A private journal plus lifecycle-owned, coalescing feedback worker."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.records: dict[str, InputRequest] = {}
        self.completed: list[str] = []
        self.confirmed_through: dict[str, int] = {}
        self.resolved_notices: list[dict[str, int]] = []
        self._notice_resolution_retry_at = 0.0
        self.loaded = False
        self.bot: Any = None
        self.task: asyncio.Task[None] | None = None
        self.events: dict[str, asyncio.Event] = {}
        self.dirty: set[tuple[int, str]] = set()
        self.clock: Callable[[], float] = time.time
        self.sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        self._next_reconcile: dict[str, float] = {}
        self._scan_positions: dict[str, int] = {}

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
        self.confirmed_through = raw.get("confirmed_through", {})
        self.resolved_notices = raw.get("resolved_notices", [])
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
                "confirmed_through": self.confirmed_through,
                "resolved_notices": self.resolved_notices,
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
            kind == "text"
            or kind == "voice"
            and isinstance(file_id, str)
            and file_id
            or follows_voice
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
        logger.info(
            "input_admitted session=%s window=%s message_id=%s kind=%s",
            request.session_id,
            request.window_id,
            request.message_id,
            request.kind,
        )
        return request

    def current(self) -> InputRequest | None:
        return self.records.get(_current.get())

    def changed(self, request: InputRequest) -> None:
        if request.kind == "voice" or request.state in ("unknown", "failed"):
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
        previous = request.state
        if previous == "unknown":
            self.dirty.add((request.user_id, request.session_id))
        if confirmed and previous == "unknown" and request.notice_id is not None:
            self.resolved_notices.append(
                {
                    "user_id": request.user_id,
                    "notice_id": request.notice_id,
                    "message_id": request.message_id,
                }
            )
        request.state = "delivered"
        request.confirmed = confirmed
        self.records.pop(request.id, None)
        self.completed.append(request.id)
        self._scan_positions.pop(request.id, None)
        self._next_reconcile.pop(request.id, None)
        self.changed(request)
        logger.info(
            "input_journal_completed session=%s message_id=%s previous=%s confirmed=%s",
            request.session_id,
            request.message_id,
            previous,
            confirmed,
        )
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
        source = self.source(request)
        request.strict_codex_proof = bool(
            source is not None
            and getattr(source, "backend", "") == "codex"
            and getattr(source, "node_id", "local") == "local"
            and not text.lstrip().startswith("/")
        )
        request.checkpoint = (
            {
                "path": str(checkpoint.path),
                "offset": checkpoint.offset,
                "backend": checkpoint.backend,
            }
            if checkpoint is not None
            else None
        )
        if checkpoint is not None and request.checkpoint is not None:
            try:
                stat = checkpoint.path.stat()
                request.checkpoint["device"] = stat.st_dev
                request.checkpoint["inode"] = stat.st_ino
            except OSError:
                pass
        if request.strict_codex_proof and request.checkpoint is None:
            from .session import WindowState

            provisional = session_manager.window_states.get(wid)
            if (
                isinstance(provisional, WindowState)
                and not provisional.session_id
                and not provisional.transcript_path
            ):
                request.checkpoint = {
                    "path": "",
                    "offset": 0,
                    "backend": "codex",
                    "unbound": True,
                }
        request.prepared = text
        request.dispatch_started_at = self.clock()
        request.state = "dispatching"
        self.changed(request)
        logger.info(
            "input_dispatch_started session=%s window=%s message_id=%s checkpoint=%s",
            request.session_id,
            wid,
            request.message_id,
            request.checkpoint is not None,
        )

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
            if request.strict_codex_proof:
                request.state = "awaiting_confirmation"
                request.strict_codex_proof = True
                request.submitted_at = self.clock()
                request.confirmation_deadline = (
                    self.clock() + _CONFIRMATION_GRACE_SECONDS
                )
                self.changed(request)
                logger.info(
                    "input_awaiting_confirmation session=%s message_id=%s checkpoint=%s",
                    request.session_id,
                    request.message_id,
                    request.checkpoint is not None,
                )
                return
            self.complete(request)
            return
        request.state = "unknown"
        request.notice = "Передача запроса агенту не подтверждена. Автоповтор остановлен; проверьте исходную сессию и /requests."
        self.changed(request)
        logger.warning(
            "input_dispatch_unknown session=%s message_id=%s",
            request.session_id,
            request.message_id,
        )
        raise SubmissionUnknown()

    def reconcile(self, request: InputRequest) -> bool:
        if not request.prepared:
            return False
        from .input_checkpoint import restore_checkpoint

        if not restore_checkpoint(self, request, self._scan_positions):
            return False
        from .bot._messages_shared import (
            _VoiceTranscriptCheckpoint,
            _transcript_contains_voice_text,
        )

        if request.checkpoint is None:
            return False
        checkpoint = _VoiceTranscriptCheckpoint(
            Path(request.checkpoint["path"]),
            request.checkpoint["offset"],
            request.checkpoint["backend"],
        )
        if checkpoint.backend == "codex" and (
            request.strict_codex_proof or request.state == "awaiting_confirmation"
        ):
            # One physical user row can acknowledge at most one request,
            # even when two identical prompts were sent before either appeared.
            base_path_key = str(checkpoint.path)
            device = request.checkpoint.get("device")
            inode = request.checkpoint.get("inode")
            path_key = (
                f"{base_path_key}#{device}:{inode}"
                if device is not None and inode is not None
                else base_path_key
            )
            legacy_offset = (
                0
                if request.checkpoint.get("replaced")
                else self.confirmed_through.get(base_path_key, 0)
            )
            start = max(
                checkpoint.offset,
                legacy_offset,
                self.confirmed_through.get(path_key, 0),
                self._scan_positions.get(request.id, 0),
            )
            try:
                if checkpoint.path.stat().st_size < start:
                    return False
                with checkpoint.path.open("rb") as stream:
                    stream.seek(start)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break
                        self._scan_positions[request.id] = stream.tell()
                        try:
                            row = json.loads(line)
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            continue
                        if (
                            not isinstance(row, dict)
                            or row.get("type") != "response_item"
                        ):
                            continue
                        payload = row.get("payload")
                        if (
                            not isinstance(payload, dict)
                            or payload.get("type") != "message"
                            or payload.get("role") != "user"
                        ):
                            continue
                        not_before = float(request.checkpoint.get("not_before", 0))
                        if not_before:
                            try:
                                stamp = datetime.fromisoformat(
                                    str(row.get("timestamp", "")).replace("Z", "+00:00")
                                ).timestamp()
                            except (TypeError, ValueError, OSError):
                                continue
                            if stamp < not_before:
                                continue
                        content = payload.get("content", [])
                        if not isinstance(content, list):
                            continue
                        text = "\n".join(
                            str(item.get("text") or "")
                            for item in content
                            if isinstance(item, dict)
                            and item.get("type") in ("input_text", "text")
                        )
                        if text.strip() == request.prepared.strip():
                            self.confirmed_through[path_key] = stream.tell()
                            self.confirmed_through[base_path_key] = stream.tell()
                            if len(self.confirmed_through) > 512:
                                self.confirmed_through.pop(
                                    next(iter(self.confirmed_through))
                                )
                            self.complete(request)
                            return True
            except OSError:
                return False
            return False
        if _transcript_contains_voice_text(checkpoint, request.prepared):
            self.complete(request)
            return True
        return False

    def check_confirmation(self, request: InputRequest) -> None:
        if request.state != "awaiting_confirmation":
            return
        now = self.clock()
        if now >= self._next_reconcile.get(request.id, 0):
            self._next_reconcile[request.id] = now + _CONFIRMATION_CHECK_SECONDS
            if self.reconcile(request):
                self._next_reconcile.pop(request.id, None)
                logger.info(
                    "input_confirmed_in_transcript session=%s message_id=%s latency_ms=%s",
                    request.session_id,
                    request.message_id,
                    round((now - request.submitted_at) * 1000)
                    if request.submitted_at
                    else None,
                )
                return
        if now >= request.confirmation_deadline:
            from .handlers import bg_status

            if bg_status.get_status(request.user_id, request.session_id) == "working":
                # Codex can queue a follow-up until the current turn or
                # compaction ends. Absence of a row while busy proves nothing.
                request.confirmation_deadline = now + _CONFIRMATION_GRACE_SECONDS
                self.save()
                return
            request.state = "unknown"
            request.notice = "Запрос принят ботом, но его получение агентом не подтверждено. Автоповтор остановлен; проверьте исходную сессию и /requests."
            self._next_reconcile.pop(request.id, None)
            self.changed(request)
            logger.warning(
                "input_confirmation_missing session=%s message_id=%s",
                request.session_id,
                request.message_id,
            )

    def reconcile_if_due(self, request: InputRequest) -> bool:
        if not request.strict_codex_proof:
            return self.reconcile(request)
        now = self.clock()
        if now < self._next_reconcile.get(request.id, 0.0):
            return False
        self._next_reconcile[request.id] = now + _CONFIRMATION_CHECK_SECONDS
        return self.reconcile(request)

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
                    if self.reconcile_if_due(request):
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
                    if request.state == "awaiting_confirmation":
                        logger.warning(
                            "input_post_dispatch_failed session=%s message_id=%s error_type=%s",
                            request.session_id,
                            request.message_id,
                            type(exc).__name__,
                        )
                        return True
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
                if request.state == "awaiting_confirmation":
                    return True
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
            if request.state in ("failed", "awaiting_confirmation"):
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
                if (
                    self.resolved_notices
                    and self.bot is not None
                    and self.clock() >= self._notice_resolution_retry_at
                ):
                    item = self.resolved_notices[0]
                    resolved = False
                    try:
                        from .telegram_rate_limit import background_telegram_request

                        with background_telegram_request():
                            await self.bot.edit_message_text(
                                chat_id=item["user_id"],
                                message_id=item["notice_id"],
                                text=f"Запрос №{item['message_id']}: получение агентом подтверждено.",
                            )
                        resolved = True
                    except BadRequest as exc:
                        resolved = (
                            "message is not modified" in str(exc).lower()
                            or "message to edit not found" in str(exc).lower()
                        )
                        if not resolved:
                            self._notice_resolution_retry_at = self.clock() + 300.0
                            logger.warning(
                                "input_notice_resolution_retry error=%s",
                                type(exc).__name__,
                            )
                    except Exception as exc:
                        self._notice_resolution_retry_at = self.clock() + max(
                            60.0, self.retry_delay(0, exc)
                        )
                        logger.warning(
                            "input_notice_resolution_retry error=%s", type(exc).__name__
                        )
                    if resolved:
                        self.resolved_notices.pop(0)
                        self.save()
                for request in tuple(self.records.values()):
                    if request.state == "awaiting_confirmation":
                        self.check_confirmation(request)
                        if request.id not in self.records:
                            continue
                    if request.state in ("failed", "unknown"):
                        if request.state == "unknown" and self.reconcile_if_due(
                            request
                        ):
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
