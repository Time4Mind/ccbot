"""Bounded background delivery of local and worker-produced files."""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from .config import config
from .file_actions import FileReference, RemoteFileReference
from .transfer_runtime import get_node_runtime

logger = logging.getLogger(__name__)


class SubmitResult(str, Enum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    USER_BUSY = "user_busy"
    STOPPED = "stopped"


class DeliveryFailure(Exception):
    def __init__(self, category: str, user_message: str):
        super().__init__(category)
        self.category = category
        self.user_message = user_message


class FileDeliveryManager:
    def __init__(
        self,
        *,
        global_limit: int = 3,
        worker_timeout: float = 120.0,
        telegram_timeout: float = 120.0,
        staging_dir: Path | None = None,
    ) -> None:
        self._semaphore = asyncio.Semaphore(max(1, global_limit))
        self._worker_timeout = max(0.01, worker_timeout)
        self._telegram_timeout = max(0.01, telegram_timeout)
        self._staging_dir = staging_dir or config.config_dir / "file-delivery"
        self._tasks: set[asyncio.Task[None]] = set()
        self._by_user: dict[int, asyncio.Task[None]] = {}
        self._by_key: dict[tuple[int, str], asyncio.Task[None]] = {}
        self._accepting = True

    def start(self) -> None:
        self._accepting = True
        try:
            self._staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._staging_dir, 0o700)
            for path in self._staging_dir.glob("delivery-*"):
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning(
                "file delivery staging cleanup failed error_type=%s",
                type(exc).__name__,
            )

    def submit(
        self,
        *,
        bot: Any,
        user_id: int,
        delivery_key: str,
        reference: FileReference,
    ) -> SubmitResult:
        key = (user_id, delivery_key)
        duplicate = self._by_key.get(key)
        if duplicate is not None and not duplicate.done():
            return SubmitResult.DUPLICATE
        active = self._by_user.get(user_id)
        if active is not None and not active.done():
            return SubmitResult.USER_BUSY
        if not self._accepting:
            return SubmitResult.STOPPED
        delivery_id = uuid.uuid4().hex[:12]
        submitted_at = time.monotonic()
        node_id = (
            reference.node_id if isinstance(reference, RemoteFileReference) else "local"
        )
        filename = Path(reference.name).name or "file"
        task = asyncio.create_task(
            self._deliver(
                bot,
                user_id,
                reference,
                delivery_id=delivery_id,
                queued_at=submitted_at,
            ),
            name=f"file-delivery:{user_id}:{delivery_id}",
        )
        self._tasks.add(task)
        self._by_user[user_id] = task
        self._by_key[key] = task
        self._log_phase(
            delivery_id,
            "callback_acknowledged",
            submitted_at,
            node_id=node_id,
            filename=filename,
        )

        def finish(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if self._by_user.get(user_id) is done:
                self._by_user.pop(user_id, None)
            if self._by_key.get(key) is done:
                self._by_key.pop(key, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("file delivery task escaped its supervisor")

        task.add_done_callback(finish)
        return SubmitResult.ACCEPTED

    def notify_user(self, *, bot: Any, user_id: int, text: str) -> None:
        task = asyncio.create_task(
            self._notify_failure(bot, user_id, text),
            name=f"file-delivery-notice:{user_id}",
        )
        self._tasks.add(task)

        def finish(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("file delivery notification escaped its supervisor")

        task.add_done_callback(finish)

    async def _deliver(
        self,
        bot: Any,
        user_id: int,
        reference: FileReference,
        *,
        delivery_id: str,
        queued_at: float,
    ) -> None:
        delivery_started = time.monotonic()
        node_id = (
            reference.node_id if isinstance(reference, RemoteFileReference) else "local"
        )
        filename = Path(reference.name).name or "file"
        staged_path: Path | None = None
        try:
            async with self._semaphore:
                self._log_phase(
                    delivery_id,
                    "queue_complete",
                    queued_at,
                    node_id=node_id,
                    filename=filename,
                )
                if isinstance(reference, RemoteFileReference):
                    staged_path, filename = await self._fetch_remote(
                        delivery_id, reference
                    )
                    source_path = staged_path
                else:
                    source_path = reference
                upload_started = time.monotonic()
                open_started = time.monotonic()
                with source_path.open("rb") as source:
                    source_stat = os.fstat(source.fileno())
                    if not stat.S_ISREG(source_stat.st_mode):
                        raise DeliveryFailure("not_regular", "Файл больше недоступен.")
                    byte_count = source_stat.st_size
                    if not isinstance(reference, RemoteFileReference):
                        self._log_phase(
                            delivery_id,
                            "local_open",
                            open_started,
                            node_id=node_id,
                            filename=filename,
                            byte_count=byte_count,
                        )
                    try:
                        await asyncio.wait_for(
                            bot.send_document(
                                chat_id=user_id,
                                document=source,
                                filename=filename,
                                disable_notification=True,
                            ),
                            timeout=self._telegram_timeout,
                        )
                    except asyncio.TimeoutError as exc:
                        raise DeliveryFailure(
                            "telegram_timeout",
                            "Telegram не подтвердил отправку файла. Проверьте "
                            "чат перед повтором.",
                        ) from exc
                    except Exception as exc:
                        raise DeliveryFailure(
                            "telegram_error",
                            "Telegram не подтвердил отправку файла. Проверьте "
                            "чат перед повтором.",
                        ) from exc
                self._log_phase(
                    delivery_id,
                    "telegram_upload",
                    upload_started,
                    node_id=node_id,
                    filename=filename,
                    byte_count=byte_count,
                )
                self._log_phase(
                    delivery_id,
                    "complete",
                    delivery_started,
                    node_id=node_id,
                    filename=filename,
                    byte_count=byte_count,
                )
        except asyncio.CancelledError:
            logger.info(
                "file_delivery_cancelled id=%s elapsed_ms=%d node=%s filename=%s",
                delivery_id,
                round((time.monotonic() - delivery_started) * 1000),
                node_id,
                Path(filename).name[:80] or "file",
            )
            raise
        except DeliveryFailure as exc:
            logger.warning(
                "file_delivery_failed id=%s category=%s elapsed_ms=%d node=%s "
                "filename=%s",
                delivery_id,
                exc.category,
                round((time.monotonic() - delivery_started) * 1000),
                node_id,
                Path(filename).name[:80] or "file",
            )
            await self._notify_failure(bot, user_id, exc.user_message)
        except (OSError, ValueError) as exc:
            logger.warning(
                "file_delivery_failed id=%s category=unavailable node=%s filename=%s "
                "elapsed_ms=%d error_type=%s",
                delivery_id,
                node_id,
                Path(filename).name[:80] or "file",
                round((time.monotonic() - delivery_started) * 1000),
                type(exc).__name__,
            )
            await self._notify_failure(
                bot,
                user_id,
                "Файл недоступен или изменился. Откройте карточку и повторите.",
            )
        except Exception as exc:
            logger.warning(
                "file_delivery_failed id=%s category=transfer node=%s filename=%s "
                "elapsed_ms=%d error_type=%s",
                delivery_id,
                node_id,
                Path(filename).name[:80] or "file",
                round((time.monotonic() - delivery_started) * 1000),
                type(exc).__name__,
            )
            await self._notify_failure(
                bot, user_id, "Не удалось отправить файл. Попробуйте ещё раз."
            )
        finally:
            if staged_path is not None:
                staged_path.unlink(missing_ok=True)

    async def _fetch_remote(
        self, delivery_id: str, reference: RemoteFileReference
    ) -> tuple[Path, str]:
        runtime = get_node_runtime(reference.node_id)
        if runtime is None:
            raise DeliveryFailure(
                "worker_offline", "Нода недоступна. Подключите её и повторите отправку."
            )
        self._staging_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._staging_dir, 0o700)
        fd, raw_path = tempfile.mkstemp(prefix="delivery-", dir=self._staging_dir)
        os.fchmod(fd, 0o600)
        path = Path(raw_path)
        fetch_started = time.monotonic()
        try:
            with os.fdopen(fd, "w+b") as destination:
                try:
                    result = await asyncio.wait_for(
                        runtime.download_session_file_to(
                            reference.node_id,
                            reference.session_id,
                            reference.path,
                            destination,
                            expected_size=reference.size,
                            expected_version=reference.version,
                        ),
                        timeout=self._worker_timeout,
                    )
                except asyncio.TimeoutError as exc:
                    raise DeliveryFailure(
                        "worker_timeout",
                        "Нода не успела передать файл. Попробуйте ещё раз.",
                    ) from exc
                destination.flush()
            filename = (
                Path(str(result.get("name", reference.name))).name
                or Path(reference.name).name
                or "file"
            )
            byte_count = path.stat().st_size
            self._log_phase(
                delivery_id,
                "worker_fetch",
                fetch_started,
                node_id=reference.node_id,
                filename=filename,
                byte_count=byte_count,
            )
            return path, filename
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    @staticmethod
    async def _notify_failure(bot: Any, user_id: int, text: str) -> None:
        try:
            await bot.send_message(
                chat_id=user_id, text=text, disable_notification=True
            )
        except Exception as exc:
            logger.warning(
                "file delivery failure notification failed error_type=%s",
                type(exc).__name__,
            )

    @staticmethod
    def _log_phase(
        delivery_id: str,
        phase: str,
        started_at: float,
        *,
        node_id: str,
        filename: str,
        byte_count: int | None = None,
    ) -> None:
        logger.info(
            "file_delivery_phase id=%s phase=%s elapsed_ms=%d node=%s "
            "filename=%s bytes=%s",
            delivery_id,
            phase,
            round((time.monotonic() - started_at) * 1000),
            node_id,
            Path(filename).name[:80] or "file",
            byte_count if byte_count is not None else "unknown",
        )

    async def wait_for_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def shutdown(self, *, timeout: float = 5.0) -> None:
        self._accepting = False
        if not self._tasks:
            return
        done, pending = await asyncio.wait(tuple(self._tasks), timeout=timeout)
        del done
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


file_delivery_manager = FileDeliveryManager(
    global_limit=config.file_delivery_concurrency,
    worker_timeout=config.file_delivery_worker_timeout,
    telegram_timeout=config.file_delivery_telegram_timeout,
)


async def shutdown_file_deliveries() -> None:
    await file_delivery_manager.shutdown()
