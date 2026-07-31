"""``/login`` and automatic agent authentication from Telegram.

The whole point is recovering from a dead OAuth login while the only device at
hand is the phone. The bot process itself never needs Claude auth, so it stays
alive to drive this even when every session is failing.

Flow: ``/login`` (or the 🔐 button on the "authorization expired" notice) →
bot spawns ``claude auth login`` and posts its URL → user approves in the phone
browser and sends the code back as an ordinary message → ``maybe_consume_code``
picks that message up instead of routing it to a session, feeds it to the
waiting process, and reports the new deadline.

Claude uses its paste-back OAuth code flow. Codex uses app-server's device-code
flow: the bot posts a URL and code, then waits for completion without consuming
the user's next Telegram message.
"""

from __future__ import annotations

import logging
import time
import asyncio

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...claude_auth import (
    credentials_state,
    drop_flow,
    get_flow,
    start_flow,
)
from ... import codex_auth
from ...config import config
from ...handlers.callback_data import CB_AUTH_CANCEL, CB_AUTH_LOGIN
from ...handlers.menu import build_footer_keyboard, render_more_text
from ...handlers.message_sender import safe_send
from ...handlers.notifications import repost_card
from ...i18n import t
from ...session import session_manager
from .._common import is_user_allowed

logger = logging.getLogger(__name__)

# One notice per bot lifetime per credential deadline — re-notifying on every
# failing event would bury the chat while every session is erroring out.
_notified_walls: set[float] = set()
_codex_watchers: set[asyncio.Task[None]] = set()


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
    if session_manager.agent_backend == "codex":
        await safe_send(bot, user_id, t(user_id, "auth.login.starting"))
        flow = await codex_auth.start_flow(user_id, command=config.codex_command)
        if flow is None:
            await safe_send(bot, user_id, t(user_id, "auth.codex.no_device_code"))
            return False
        await safe_send(
            bot,
            user_id,
            t(
                user_id,
                "auth.codex.device",
                url=flow.verification_url,
                code=flow.user_code,
            ),
            reply_markup=_cancel_keyboard(user_id),
        )
        task = asyncio.create_task(_watch_codex_login(bot, user_id, flow))
        _codex_watchers.add(task)
        task.add_done_callback(_codex_watchers.discard)
        return True

    await safe_send(bot, user_id, t(user_id, "auth.login.starting"))
    flow = await start_flow(user_id, command=config.claude_command)
    if flow is None:
        await safe_send(bot, user_id, t(user_id, "auth.login.no_url"))
        return False
    # No ``disable_web_page_preview`` here: safe_send already defaults
    # ``link_preview_options`` and PTB raises ValueError when both are given —
    # which would have killed the one message the whole flow depends on.
    await safe_send(
        bot,
        user_id,
        t(user_id, "auth.login.url", url=flow.url),
        reply_markup=_cancel_keyboard(user_id),
    )
    return True


async def _watch_codex_login(
    bot: Bot, user_id: int, flow: codex_auth.LoginFlow
) -> None:
    ok, detail = await flow.wait_completed()
    codex_auth.finish_flow(user_id, flow)
    if ok:
        _notified_walls.clear()
        await safe_send(bot, user_id, t(user_id, "auth.codex.ok"))
        await _restore_working_surface(bot, user_id)
        return
    if detail != "cancelled":
        await safe_send(
            bot,
            user_id,
            t(user_id, "auth.codex.failed", detail=detail),
        )


async def ensure_codex_authenticated(bot: Bot, user_id: int) -> bool:
    """Return True when Codex can run, otherwise ensure a login is underway."""
    if session_manager.agent_backend != "codex":
        return True
    # Managed ChatGPT credentials are refreshable. A plain account/read can
    # report account=null as soon as the cached ID token expires even while a
    # valid refresh token is present. Force the official silent refresh before
    # deciding that an interactive device flow is necessary.
    state = await codex_auth.read_account_state(
        refresh_token=True, command=config.codex_command
    )
    if state is not None and state.authenticated:
        return True
    stored = await codex_auth.stored_login_available(command=config.codex_command)
    if stored is True:
        # account/read can lag or reject an expired ID token while the
        # effective credential store is still valid for a new Codex process.
        return True
    if stored is None and codex_auth.has_cached_managed_credentials():
        # An inconclusive Keychain probe is not permission to replace auth.
        return True
    if codex_auth.get_flow(user_id) is None:
        await start_login(bot, user_id)
    else:
        await safe_send(bot, user_id, t(user_id, "auth.codex.waiting"))
    return False


async def ensure_codex_auth_on_start(bot: Bot) -> None:
    """On a fresh Codex host, start device login as soon as the bot is online."""
    if session_manager.agent_backend != "codex":
        return
    # Do not turn a routinely expired cached token into a browser-login alert.
    # app-server owns managed ChatGPT token rotation and refreshes auth.json.
    state = await codex_auth.read_account_state(
        refresh_token=True, command=config.codex_command
    )
    if state is not None and state.authenticated:
        logger.info(
            "Codex account ready (type=%s, plan=%s)",
            state.account_type,
            state.plan_type,
        )
        return
    stored = await codex_auth.stored_login_available(command=config.codex_command)
    cached_managed_credentials = codex_auth.has_cached_managed_credentials()
    if state is None:
        if stored is True or (stored is None and cached_managed_credentials):
            logger.warning(
                "Codex auth preflight was unavailable; credential storage "
                "may still be usable, so startup login is deferred"
            )
            return
        logger.warning(
            "Codex auth preflight was unavailable and effective storage "
            "reports no login; starting device flow"
        )
    if stored is True:
        logger.warning(
            "Codex account/read remained unauthenticated after silent refresh; "
            "the effective credential store is logged in, so startup login "
            "will not replace it"
        )
        return
    if stored is None and cached_managed_credentials:
        logger.warning(
            "Codex credential-store probe was inconclusive; cached managed "
            "credentials are present, so startup login will not replace them"
        )
        return
    logger.info("Codex account is not authenticated; starting device flow")
    for user_id in sorted(config.allowed_users):
        await start_login(bot, user_id)


async def shutdown_auth_flows() -> None:
    """Cancel login children/watchers during bot shutdown."""
    await codex_auth.cancel_all_flows()
    watchers = list(_codex_watchers)
    for task in watchers:
        task.cancel()
    if watchers:
        await asyncio.gather(*watchers, return_exceptions=True)
    _codex_watchers.clear()


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
        await _restore_working_surface(context.bot, user.id)
    else:
        await safe_send(
            context.bot, user.id, t(user.id, "auth.login.failed", detail=detail)
        )
    return True


async def _restore_working_surface(bot: Bot, user_id: int) -> None:
    """Put a usable surface back under the confirmation message.

    A bare "✅ logged in" leaves the user staring at a dead end: the last live
    card is buried above the notice / link / code exchange, so they'd have to
    scroll back to reach the switcher and footer. Repost the active session's
    card instead (it carries header, body, bg-panel, switcher and footer), or
    the Menu when no session is active.
    """
    sess = session_manager.get_active_session(user_id)
    if sess is not None and sess.window_id:
        try:
            await repost_card(bot, user_id, sess)
            return
        except Exception as exc:  # noqa: BLE001 — fall back to the Menu
            logger.debug("post-login card repost failed: %s", exc)
    text = render_more_text(user_id)
    keyboard = build_footer_keyboard(user_id, screen="more")
    sent = await safe_send(bot, user_id, text, reply_markup=keyboard)
    if sent is not None and keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)


async def notify_auth_expired(bot: Bot, user_id: int) -> None:
    """Tell the user the login is gone and offer the 🔐 button. Deduped."""
    state = credentials_state()
    wall = state.refresh_expires_at or 0.0
    if wall in _notified_walls:
        logger.debug("auth-expired notice already sent for wall=%s", wall)
        return
    _notified_walls.add(wall)
    logger.info(
        "auth_expired_notice user=%d wall=%s",
        user_id,
        _fmt_deadline(state.refresh_expires_at),
        extra={
            "event": "auth_expired_notice",
            "user_id": user_id,
            "wall": state.refresh_expires_at,
        },
    )
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "btn.login"), callback_data=CB_AUTH_LOGIN)]]
    )
    await safe_send(bot, user_id, t(user_id, "auth.expired"), reply_markup=keyboard)
