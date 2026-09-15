"""Telegram rate limiter with persistent operation-scoped flood cooldowns."""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import timedelta
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import time
from typing import Any

from telegram.error import RetryAfter
from telegram.ext import AIORateLimiter

logger = logging.getLogger(__name__)
_is_background_request: ContextVar[bool] = ContextVar(
    "ccbot_background_telegram_request", default=False
)


@contextlib.contextmanager
def background_telegram_request():
    """Mark optional background traffic for cooldown-aware dropping."""
    token = _is_background_request.set(True)
    try:
        yield
    finally:
        _is_background_request.reset(token)


class PersistentEndpointRateLimiter(AIORateLimiter):
    """Keep Telegram flood waits local to the exact operation that hit them.

    PTB's retry implementation clears one process-wide event while sleeping.
    A multi-hour ``RetryAfter`` on a background card edit consequently blocks
    unrelated callback answers and messages too.  We make one attempt, store
    its deadline by token/endpoint/chat/message, and immediately re-raise.
    Calls for other operations continue through the regular PTB buckets.
    """

    def __init__(self, *, token: str, cooldown_path: Path) -> None:
        super().__init__(max_retries=0)
        self._token_scope = hashlib.sha256(token.encode()).hexdigest()[:16]
        self._cooldown_path = cooldown_path
        self._cooldowns = self._load_cooldowns()

    @staticmethod
    def _retry_seconds(exc: RetryAfter) -> float:
        raw = exc.retry_after
        if isinstance(raw, timedelta):
            return raw.total_seconds()
        return float(raw)

    def _operation_key(self, endpoint: str, data: dict[str, Any]) -> str:
        chat_id = data.get("chat_id", "-")
        message_id = data.get("message_id", "-")
        return f"{self._token_scope}|{endpoint.lower()}|{chat_id}|{message_id}"

    def _load_cooldowns(self) -> dict[str, float]:
        try:
            payload = json.loads(self._cooldown_path.read_text(encoding="utf-8"))
            raw = payload.get("cooldowns", {})
            now = time.time()
            return {
                str(key): float(deadline)
                for key, deadline in raw.items()
                if float(deadline) > now
            }
        except FileNotFoundError:
            return {}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not load Telegram cooldown state path=%s err=%s",
                self._cooldown_path,
                exc,
            )
            return {}

    def _save_cooldowns(self) -> None:
        now = time.time()
        self._cooldowns = {
            key: deadline
            for key, deadline in self._cooldowns.items()
            if deadline > now
        }
        self._cooldown_path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(
            prefix=f".{self._cooldown_path.name}.",
            dir=self._cooldown_path.parent,
            text=True,
        )
        tmp_path = Path(raw_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"version": 1, "cooldowns": self._cooldowns},
                    handle,
                    sort_keys=True,
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._cooldown_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp_path.unlink()

    async def process_request(
        self,
        callback: Any,
        args: Any,
        kwargs: dict[str, Any],
        endpoint: str,
        data: dict[str, Any],
        rate_limit_args: int | None,  # noqa: ARG002
    ) -> Any:
        key = self._operation_key(endpoint, data)
        now = time.time()
        remaining = self._cooldowns.get(key, 0.0) - now
        if remaining > 0 and _is_background_request.get():
            logger.debug(
                "Telegram operation cooling down endpoint=%s chat=%s message=%s "
                "retry_after=%.1f",
                endpoint,
                data.get("chat_id"),
                data.get("message_id"),
                remaining,
            )
            raise RetryAfter(max(1, math.ceil(remaining)))

        chat_id = data.get("chat_id")
        chat = chat_id is not None
        if chat_id is not None:
            with contextlib.suppress(ValueError, TypeError):
                chat_id = int(chat_id)
        group: int | str | bool = False
        if (isinstance(chat_id, int) and chat_id < 0) or isinstance(chat_id, str):
            group = chat_id

        try:
            result = await self._run_request(
                chat=chat,
                group=group,
                allow_paid_broadcast=bool(data.get("allow_paid_broadcast", False)),
                callback=callback,
                args=args,
                kwargs=kwargs,
            )
            if key in self._cooldowns:
                # A foreground action is allowed one immediate probe. Success
                # proves Telegram released this concrete operation early.
                self._cooldowns.pop(key, None)
                self._save_cooldowns()
            return result
        except RetryAfter as exc:
            seconds = self._retry_seconds(exc)
            self._cooldowns[key] = time.time() + seconds
            self._save_cooldowns()
            logger.warning(
                "Telegram flood cooldown stored endpoint=%s chat=%s message=%s "
                "attempt=1 retry_after=%.1f",
                endpoint,
                data.get("chat_id"),
                data.get("message_id"),
                seconds,
            )
            raise
