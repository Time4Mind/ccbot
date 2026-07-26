"""``/login`` — re-authenticate Claude Code from the chat.

The whole point is recovering from a dead OAuth login while the only device at
hand is the phone. The bot process itself never needs Claude auth, so it stays
alive to drive this even when every session is failing.

Flow: ``/login`` (or the 🔐 button on the "authorization expired" notice) →
bot spawns ``claude auth login`` and posts its URL → user approves in the phone
browser and sends the code back as an ordinary message → ``maybe_consume_code``
picks that message up instead of routing it to a session, feeds it to the
waiting process, and reports the new deadline.

Key components: login_command, maybe_consume_code, notify_auth_expired.
"""

from __future__ import annotations

import logging
import time

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude_auth import (
    credentials_state,
    drop_flow,
    get_flow,
    start_flow,
)
from ...config import config
from ...handlers.callback_data import CB_AUTH_CANCEL, CB_AUTH_LOGIN
from ...handlers.message_sender import safe_send
from ...i18n import t
from .._common import is_user_allowed

logger = logging.getLogger(__name__)

# One notice per bot lifetime per credential deadline — re-notifying on every
# failing event would bury the chat while every session is erroring out.
_notified_walls: set[float] = set()


def _fmt_deadline(ts: float | None) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _cancel_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "btn.cancel"), callback_data=CB_AUTH_CANCEL)]]
    )


async def start_login(bot: Bot, user_id: int) -> bool:
    """Spawn the login exchange and post the URL. True when the URL went out."""
    await safe_send(bot, user_id, t(user_id, "auth.login.starting"))
    flow = await start_flow(user_id, command=config.claude_command)
    if flow is None:
        await safe_send(bot, user_id, t(user_id, "auth.login.no_url"))
        return False
    await safe_send(
        bot,
        user_id,
        t(user_id, "auth.login.url", url=flow.url),
        reply_markup=_cancel_keyboard(user_id),
        disable_web_page_preview=True,
    )
    return True


async def login_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/login — hand out a fresh OAuth URL and wait for the code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    await start_login(context.bot, user.id)


async def maybe_consume_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Treat this message as the pasted OAuth code when a flow is waiting.

    Returns True when the message was consumed (so the caller must NOT route it
    to a Claude session — the code is not a prompt).
    """
    user = update.effective_user
    # ``update.message`` (not ``effective_message``) to match the rest of the
    # bot's handlers — text_handler is only wired for plain messages.
    message = update.message
    if not user or message is None:
        return False
    flow = get_flow(user.id)
    if flow is None:
        return False

    code = (message.text or "").strip()
    if not code or code.startswith("/"):
        return False

    ok, detail = await flow.submit_code(code)
    drop_flow(user.id)
    # The code is a single-use credential; don't leave it sitting in the chat.
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 — best effort, TG may forbid it
        logger.debug("could not delete the pasted auth code: %s", exc)

    state = credentials_state()
    if ok:
        _notified_walls.clear()
        await safe_send(
            context.bot,
            user.id,
            t(
                user.id,
                "auth.login.ok",
                deadline=_fmt_deadline(state.refresh_expires_at),
            ),
        )
    else:
        await safe_send(
            context.bot, user.id, t(user.id, "auth.login.failed", detail=detail)
        )
    return True


async def notify_auth_expired(bot: Bot, user_id: int) -> None:
    """Tell the user the login is gone and offer the 🔐 button. Deduped."""
    state = credentials_state()
    wall = state.refresh_expires_at or 0.0
    if wall in _notified_walls:
        return
    _notified_walls.add(wall)
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "btn.login"), callback_data=CB_AUTH_LOGIN)]]
    )
    await safe_send(bot, user_id, t(user_id, "auth.expired"), reply_markup=keyboard)
