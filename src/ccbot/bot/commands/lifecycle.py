"""Session lifecycle slash commands: /new, /kill, /stop, /menu, /archive."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...handlers.archive import (
    DEFAULT_LOOKBACK_SECONDS,
    archive_or_delete_session,
    build_archive_page,
)
from ...handlers.callback_data import (
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
)
from ...handlers.cleanup import teardown_session_runtime
from ...session_models import reserve_owner
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_reply
from ...i18n import t
from ...session import Session, session_manager
from ...tmux_manager import tmux_manager
from ...startup_queue import begin_startup_queue, bind_startup_queue
from .._common import (
    active_window,
    is_user_allowed,
    resolve_ident,
)

logger = logging.getLogger(__name__)


# --- /new ---


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/new [name] [path]` — create a new session.

    With no args, opens the directory browser.
    With one arg, treats it as the session name and browses for path.
    With two args, creates the session immediately at the given path.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    # Open capture before authentication, filesystem and Telegram awaits.
    # Non-blocking voice handlers can otherwise race into the old session.
    begin_startup_queue(user.id)

    args = (update.message.text or "").split(maxsplit=2)
    name_arg = args[1] if len(args) > 1 else ""
    path_arg = args[2] if len(args) > 2 else ""
    node_id = session_manager.get_selected_node_id(user.id)
    enabled_backends = session_manager.get_effective_backends(user.id, node_id)

    if path_arg:
        if node_id != "local":
            from ...startup_queue import cancel_startup_queue

            cancel_startup_queue(user.id)
            await safe_reply(
                update.message,
                "❌ A path cannot be resolved on a remote node from this command. "
                "Use /new and choose the directory on that node.",
            )
            return
        if not enabled_backends:
            from ...startup_queue import cancel_startup_queue

            cancel_startup_queue(user.id)
            await safe_reply(update.message, "❌ No available backend on local node")
            return
        preferred_backend = session_manager.get_default_backend(user.id)
        backend = (
            preferred_backend
            if preferred_backend in enabled_backends
            else enabled_backends[0]
        )
        target_path = str(Path(path_arg).expanduser().resolve())
        if backend == "codex":
            from .auth import ensure_codex_authenticated

            if not await ensure_codex_authenticated(context.bot, user.id):
                await safe_reply(
                    update.message,
                    t(user.id, "auth.codex.required"),
                )
                return
        await safe_reply(update.message, f"⏳ Creating session at {target_path}…")
        success, message, created_wname, created_wid = await tmux_manager.create_window(
            target_path,
            backend=backend,
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        session_manager.mark_window_starting(
            created_wid,
            backend=backend,
            resume=False,
            bot=context.bot,
            user_id=user.id,
        )
        sess = session_manager.create_session(
            name=name_arg or created_wname or "",
            window_id=created_wid,
            workdir=target_path,
            backend=backend,
        )
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id:
            session_manager.set_session_claude_id(sess.id, ws.session_id)
        session_manager.set_active_session(user.id, sess.id)
        bind_startup_queue(user.id, created_wid)
        await safe_reply(
            update.message,
            f"✅ Session `{sess.name}` ({sess.id}) created at {target_path}",
        )
        return

    # No path → directory browser.
    if name_arg and context.user_data is not None:
        context.user_data["_pending_session_name"] = name_arg
    only_backend = next(iter(enabled_backends), None)
    if only_backend is None:
        await safe_reply(update.message, "❌ No enabled backend")
        return
    if len(enabled_backends) > 1:
        from ..callbacks.dir_browser import build_backend_picker

        if context.user_data is not None:
            context.user_data["menu_origin"] = "main"
            context.user_data["_new_session_node_id"] = node_id
        await safe_reply(
            update.message,
            t(user.id, "backend.choose"),
            reply_markup=build_backend_picker(user.id, node_id=node_id),
        )
        return
    if context.user_data is not None:
        context.user_data["_new_session_backend"] = only_backend
        context.user_data["_new_session_node_id"] = node_id
    from ..callbacks.dir_browser import initialize_directory_browser

    msg_text, keyboard, _subdirs = await initialize_directory_browser(
        context, user.id, node_id=node_id
    )
    if context.user_data is not None:
        context.user_data["menu_origin"] = "main"
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


# --- /kill ---


async def archive_session(user_id: int, bot: Bot, sess: Session) -> None:
    """Kill the tmux window if alive and archive the session."""
    was_default_reserve = reserve_owner(sess) == user_id
    await teardown_session_runtime(user_id, sess, bot)
    await archive_or_delete_session(sess, completed=False)
    if was_default_reserve:
        from ...default_session import ensure_default_session

        asyncio.create_task(ensure_default_session(bot, user_id))


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/kill [<name-or-id>]` — stop and archive after confirmation.

    Without an argument, applies to the user's active session.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split(maxsplit=1)
    if len(args) >= 2:
        sess = resolve_ident(args[1].strip())
    else:
        sess = session_manager.get_active_session(user.id)
    if sess is None or sess.state not in ("active", "idle", "lost"):
        await safe_reply(update.message, "❌ Session not found or already archived.")
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t(user.id, "btn.yes_kill"),
                    callback_data=f"{CB_CONF_KILL_YES}{sess.id}"[:64],
                ),
                InlineKeyboardButton(
                    t(user.id, "btn.no"), callback_data=CB_CONF_KILL_NO
                ),
            ]
        ]
    )
    await safe_reply(
        update.message, t(user.id, "conf.kill", name=sess.name), reply_markup=kb
    )


# --- /stop ---


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/stop` — send Escape to the active session's tmux window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = active_window(user.id)
    if not wid:
        await safe_reply(update.message, "❌ No active session.")
        return
    sess = session_manager.find_session_by_window(wid)
    if sess is not None and sess.node_id != "local":
        from ...transfer_runtime import get_node_runtime

        runtime = get_node_runtime(sess.node_id)
        routing_id = sess.worker_session_id or sess.claude_session_id
        if runtime is None or not routing_id:
            await safe_reply(update.message, "❌ Remote session is unavailable.")
            return
        try:
            result = await runtime.send_key(sess.node_id, routing_id, "Escape")
        except Exception:
            logger.exception("Remote stop failed for session %s", sess.id)
            result = {"ok": False}
        await safe_reply(
            update.message,
            "⎋ Sent Escape" if result.get("ok") else "❌ Failed to send Escape",
        )
        return
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return
    await tmux_manager.send_keys(w.window_id, "\x1b", enter=False)
    await safe_reply(update.message, "⎋ Sent Escape")


# --- /menu ---


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/menu` — global entry point: opens the Menu screen as a fresh message."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    from ..callbacks.more_menu import begin_menu_refresh, render_menu_text

    text = render_menu_text(user.id, refreshing=True)
    keyboard = build_footer_keyboard(user.id, screen="more")
    sent = await safe_reply(update.message, text, reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user.id, sent.message_id)
        begin_menu_refresh(sent, user.id)


# --- /archive ---


async def archive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/archive` — paginated list of archived sessions.

    The archive is one unified 20-day list.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    text, keyboard = await build_archive_page(
        page=0,
        lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
        show_all=False,
        user_id=user.id,
    )
    await safe_reply(update.message, text, reply_markup=keyboard)
