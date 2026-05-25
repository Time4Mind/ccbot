"""Per-user throttle on send_chat_action(TYPING).

Telegram's typing indicator stays visible for ~5 s after one
``send_chat_action`` call. Two of our call sites — ``status_polling``
(every 1 s while a session is busy) and ``session_events`` (every
inbound claude event) — used to re-fire the action many times per
second during heavy tool sequences. Each call counts toward
Telegram's per-chat rate budget and was a measurable contributor to
the 429 ``Retry after 71 s`` bans seen during heavy multi-session
usage.

``fire_typing`` collapses all callers behind a single per-user
timestamp. A call within ``TYPING_REFRESH_INTERVAL`` of the last
successful fire for the same user is a silent no-op — the indicator
is still on, no API call needed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from telegram import Bot
from telegram.constants import ChatAction

logger = logging.getLogger(__name__)

# Telegram refreshes the indicator on every chat-action; one call
# keeps it visible for ~5 s. We refresh at 4 s so the indicator
# stays solid for a steadily-emitting session, but every call within
# 4 s of the last one is dropped — that's the entire point.
TYPING_REFRESH_INTERVAL = 4.0

_last_fired: dict[int, float] = {}


async def fire_typing(
    bot: Bot,
    user_id: int,
    source: str,
    **extra: Any,
) -> bool:
    """Fire ``send_chat_action(TYPING)`` for ``user_id``, throttled.

    Returns ``True`` iff the chat-action was actually sent. Calls
    within ``TYPING_REFRESH_INTERVAL`` of the last successful fire
    for this user are dropped silently (the indicator is still
    showing — there's nothing to log).

    ``source`` and ``**extra`` are stamped onto the structured
    ``typing_fired`` log record for parity with the prior per-site
    logging.
    """
    now = time.monotonic()
    last = _last_fired.get(user_id, 0.0)
    if now - last < TYPING_REFRESH_INTERVAL:
        return False
    try:
        await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    except Exception as e:
        logger.debug("send_chat_action TYPING failed: %s", e)
        return False
    _last_fired[user_id] = now
    pretty = " ".join(f"{k}={v}" for k, v in extra.items())
    logger.info(
        "typing_fired source=%s user=%d %s",
        source,
        user_id,
        pretty,
        extra={
            "event": "typing_fired",
            "source": source,
            "user_id": user_id,
            **extra,
        },
    )
    return True
