"""Session lifecycle slash commands: /new, /list, /kill, /done, /stop,
/menu, /archive.

The /list rendering helpers (``build_live_sessions_text``, ``shorten_workdir``)
live here too because they're also used by the Menu→List callback path.
``archive_session`` is shared between /kill and /done.
"""

from __future__ import annotations

import logging
from pathlib import Path

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...config import config
from ...handlers.archive import DEFAULT_LOOKBACK_SECONDS, build_archive_page
from ...handlers.callback_data import (
    CB_CONF_DONE_NO,
    CB_CONF_DONE_YES,
    CB_CONF_KILL_NO,
    CB_CONF_KILL_YES,
)
from ...handlers.cleanup import clear_session_state
from ...handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    build_directory_browser,
    clear_browse_state,
)
from ...handlers.menu import build_footer_keyboard, render_more_text
from ...handlers.message_sender import safe_reply
from ...handlers.switcher import build_switcher_keyboard
from ...i18n import t
from ...session import Session, session_manager
from ...tmux_manager import tmux_manager
from .._common import (
    active_window,
    is_user_allowed,
    resolve_ident,
    shorten_workdir,
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

    args = (update.message.text or "").split(maxsplit=2)
    name_arg = args[1] if len(args) > 1 else ""
    path_arg = args[2] if len(args) > 2 else ""

    if path_arg:
        target_path = str(Path(path_arg).expanduser().resolve())
        await safe_reply(update.message, f"⏳ Creating session at {target_path}…")
        success, message, created_wname, created_wid = await tmux_manager.create_window(
            target_path
        )
        if not success:
            await safe_reply(update.message, f"❌ {message}")
            return
        hook_ok = await session_manager.wait_for_session_map_entry(
            created_wid, timeout=5.0
        )
        del hook_ok
        sess = session_manager.create_session(
            name=name_arg or created_wname or "",
            window_id=created_wid,
            workdir=target_path,
        )
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id:
            session_manager.set_session_claude_id(sess.id, ws.session_id)
        session_manager.set_active_session(user.id, sess.id)
        if session_manager.get_user_settings(user.id).get("local_terminal") == "on":
            from ...local_terminal import open_terminal_for_window

            await open_terminal_for_window(created_wid)
        await safe_reply(
            update.message,
            f"✅ Session `{sess.name}` ({sess.id}) created at {target_path}",
        )
        return

    # No path → directory browser.
    if name_arg and context.user_data is not None:
        context.user_data["_pending_session_name"] = name_arg
    clear_browse_state(context.user_data)
    start_path = str(Path.home())
    msg_text, keyboard, subdirs = build_directory_browser(start_path)
    if context.user_data is not None:
        context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        context.user_data[BROWSE_PATH_KEY] = start_path
        context.user_data[BROWSE_PAGE_KEY] = 0
        context.user_data[BROWSE_DIRS_KEY] = subdirs
        context.user_data["menu_origin"] = "main"
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


# --- /list (text + helpers used by the Menu→List callback as well) ---


def build_live_sessions_text(user_id: int) -> str | None:
    """Render the same body /list shows. None when there are no live sessions."""
    sessions = session_manager.list_user_sessions(
        user_id, states=("active", "idle", "lost")
    )
    if not sessions:
        return None
    active_sess = session_manager.get_active_session(user_id)
    active_id = active_sess.id if active_sess else ""
    active_block: list[str] = []
    lost_block: list[str] = []
    for s in sessions:
        usage = (
            f"{s.token_usage_total // 1000}k tok" if s.token_usage_total else "0 tok"
        )
        wd = shorten_workdir(s.workdir)
        if s.state == "lost":
            lost_block.append(f"  • *{s.name}* — {usage} · `{wd}`")
        else:
            marker = "✓ " if s.id == active_id else "  "
            active_block.append(f"{marker}*{s.name}* — {usage} · `{wd}`")
    lines: list[str] = []
    if active_block:
        lines.append(t(user_id, "list.active"))
        lines.extend(active_block)
    if lost_block:
        if active_block:
            lines.append("")
        lines.append(t(user_id, "list.lost"))
        lines.extend(lost_block)
    return "\n".join(lines)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/list` — show all live sessions with state and short usage."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    body = build_live_sessions_text(user.id)
    if body is None:
        await safe_reply(update.message, t(user.id, "list.empty"))
        return

    keyboard = build_switcher_keyboard(user.id, include_lost=True)
    sent = await safe_reply(update.message, body, reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user.id, sent.message_id)


# --- /kill, /done — share archive_session ---


async def archive_session(
    user_id: int, bot: Bot, sess: Session, *, completed: bool
) -> None:
    """Kill tmux window if alive and mark the Session archived/completed.

    Used by both the /kill confirmation and the /done confirmation, plus
    the matching CB_CONF_*_YES callback paths.
    """
    wid = sess.window_id
    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
        await clear_session_state(user_id, wid, bot)
    session_manager.mark_session_archived(sess.id, completed=completed)


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


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/done [<name-or-id>]` — mark goal achieved and archive after confirm.

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
    if sess is None or sess.state not in ("active", "idle"):
        await safe_reply(update.message, "❌ Session not found or not live.")
        return
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t(user.id, "btn.confirm"),
                    callback_data=f"{CB_CONF_DONE_YES}{sess.id}"[:64],
                ),
                InlineKeyboardButton(
                    t(user.id, "btn.no"), callback_data=CB_CONF_DONE_NO
                ),
            ]
        ]
    )
    await safe_reply(
        update.message, t(user.id, "conf.done", name=sess.name), reply_markup=kb
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
    text = render_more_text(user.id)
    keyboard = build_footer_keyboard(user.id, screen="more")
    sent = await safe_reply(update.message, text, reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user.id, sent.message_id)


# --- /archive ---


async def archive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/archive` — paginated list of archived sessions.

    `--all` flag extends lookback to ARCHIVE_PURGE_AFTER (default 14d).
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split()
    show_all = "--all" in args
    lookback = config.archive_purge_after if show_all else DEFAULT_LOOKBACK_SECONDS
    if context.user_data is not None:
        context.user_data["_arc_show_all"] = show_all

    text, keyboard = build_archive_page(
        page=0, lookback_seconds=lookback, show_all=show_all
    )
    await safe_reply(update.message, text, reply_markup=keyboard)
