"""Telegram bot handlers — the main UI layer of ccbot (DM mode).

Registers all command/callback/message handlers and manages the bot lifecycle.
The bot runs in a private 1-1 chat. Each user has multiple parallel sessions;
inbound text routes to the active session (`session_manager.active_sessions`).

Core responsibilities:
  - Command handlers: /start, /history, /screenshot, /esc, /usage, plus
    forwarding unknown /commands to Claude Code in the active session.
  - Callback query handler: directory browser, history pagination,
    interactive UI navigation, screenshot refresh.
  - DM routing: inbound text → active session's tmux window. Reply-quote
    (Phase 2) routes one message to a non-active session.
  - Photo handling: photos sent by user are downloaded and forwarded
    to Claude Code in the active session as file paths.
  - Voice handling: voice messages are transcribed (Whisper / OpenAI)
    and forwarded as text to the active session.
  - Bot lifecycle management: post_init, post_shutdown, create_bot.

Handler modules (in handlers/):
  - callback_data: Callback data constants
  - message_queue: Per-user message queue management
  - message_sender: Safe message sending helpers
  - history: Message history pagination
  - directory_browser: Directory browser UI
  - interactive_ui: Interactive UI handling
  - status_polling: Terminal status polling
  - response_builder: Response message building

Key functions: create_bot(), handle_new_message().
"""

import asyncio
import io
import logging
from pathlib import Path

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaDocument,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.archive import (
    DEFAULT_LOOKBACK_SECONDS,
    build_archive_page,
    restore_session,
)
from .handlers.callback_data import (
    CB_ARC_ALL,
    CB_ARC_BACK,
    CB_ARC_DELETE,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
    CB_ARC_RESTORE,
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_SW_NEW,
    CB_SW_NOOP,
    CB_SW_USE,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    clear_browse_state,
    clear_session_picker_state,
    clear_window_picker_state,
)
from .handlers.cleanup import clear_session_state
from .handlers.history import send_history
from .handlers.inbox import save_inbox_file
from .handlers.interactive_ui import (
    INTERACTIVE_TOOL_NAMES,
    clear_interactive_mode,
    clear_interactive_msg,
    get_interactive_msg_id,
    get_interactive_window,
    handle_interactive_ui,
    set_interactive_mode,
)
from .handlers.message_queue import (
    clear_status_msg_info,
    get_message_queue,
    shutdown_workers,
)
from .handlers.message_sender import (
    NO_LINK_PREVIEW,
    safe_edit,
    safe_reply,
    safe_send,
    send_with_fallback,
)
from .handlers.notifications import (
    finalize_task,
    lookup_session_for_message,
    push_event,
    update_session_card,
)
from .markdown_v2 import convert_markdown
from .naming import maybe_auto_name
from .handlers.status_polling import status_poll_loop
from .handlers.switcher import (
    build_session_preview,
    build_switcher_keyboard,
)
from .screenshot import text_to_image
from .session import Session, session_manager
from .session_monitor import NewMessage, SessionMonitor
from .usage import (
    aggregate_session,
    compute_user_usage,
    format_usage_status,
    should_warn_quota,
)
from .terminal_parser import extract_bash_output, is_interactive_ui
from .tmux_manager import tmux_manager
from .transcribe import close_client as close_transcribe_client
from .transcribe import transcribe_voice
from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Session monitor instance
session_monitor: SessionMonitor | None = None

# Status polling task
_status_poll_task: asyncio.Task | None = None

# Claude Code commands shown in bot menu (forwarded via tmux).
# Order preserved in setMyCommands. Dropped: /cost (duplicate of /status,
# which actually renders output), /help (meta, produces no useful TG output).
CC_COMMANDS: dict[str, str] = {
    "model": "↗ Switch AI model",
    "effort": "↗ Set thinking effort",
    "clear": "↗ Clear conversation history",
    "compact": "↗ Compact conversation context",
    "memory": "↗ Edit CLAUDE.md",
}


def is_user_allowed(user_id: int | None) -> bool:
    return user_id is not None and config.is_user_allowed(user_id)


def _active_window(user_id: int) -> str | None:
    """Return the user's active tmux window_id, or None if none active."""
    return session_manager.get_active_window(user_id)


async def _render_session_preview(sess) -> str:
    """Build a context-preview text for a Session — header + last user/assistant
    + last tool calls. Read from the session's JSONL transcript.
    """
    last_user = ""
    last_assistant = ""
    last_tools: list[str] = []
    if sess.window_id:
        try:
            messages, _total = await session_manager.get_recent_messages(sess.window_id)
        except Exception as e:
            logger.debug("preview: get_recent_messages failed: %s", e)
            messages = []
        # Walk from newest to oldest collecting one of each kind.
        for m in reversed(messages):
            role = m.get("role")
            ctype = m.get("content_type", "text")
            text = m.get("text", "")
            if not text:
                continue
            if role == "user" and not last_user:
                last_user = text
            elif role == "assistant" and ctype == "text" and not last_assistant:
                last_assistant = text
            elif (
                ctype == "tool_use"
                and len(last_tools) < config.preview_tools
            ):
                first_line = text.splitlines()[0] if text else ""
                last_tools.append(f"[tool] {first_line}")
            if (
                last_user
                and last_assistant
                and len(last_tools) >= config.preview_tools
            ):
                break
    return build_session_preview(
        sess,
        last_user=last_user,
        last_assistant=last_assistant,
        last_tools=list(reversed(last_tools)),
    )


# --- Command handlers ---


# /start removed — first text message in an empty DM already opens the
# directory browser, so there's no useful welcome screen to maintain.


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = _active_window(user.id)
    if not wid:
        await safe_reply(update.message, "❌ No active session. Use /new to create one.")
        return

    await send_history(update.message, wid)


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the current tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = _active_window(user.id)
    if not wid:
        await safe_reply(update.message, "❌ No active session. Use /new to create one.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_reply(update.message, "❌ Failed to capture pane content.")
        return

    png_bytes = await text_to_image(text, with_ansi=True)
    keyboard = _build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


def _resolve_ident(ident: str) -> Session | None:
    """Resolve a /command argument that may be a session id (short hex) or a name.

    Falls back to a case-insensitive name match on active+idle sessions. Returns
    None when nothing matches.
    """
    if not ident:
        return None
    sess = session_manager.get_session(ident)
    if sess is not None and sess.state in ("active", "idle", "archived", "completed"):
        return sess
    needle = ident.casefold()
    for s in session_manager.sessions.values():
        if s.name.casefold() == needle:
            return s
    return None


# /esc removed — duplicate of /stop (both send Escape to active session).


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from TUI and send to Telegram."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = _active_window(user.id)
    if not wid:
        await safe_reply(update.message, "No active session. Use /new to create one.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    # Send /usage command to Claude Code TUI
    await tmux_manager.send_keys(w.window_id, "/usage")
    # Wait for the modal to render
    await asyncio.sleep(2.0)
    # Capture the pane content
    pane_text = await tmux_manager.capture_pane(w.window_id)
    # Dismiss the modal
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    # Try to parse structured usage info
    from .terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        # Fallback: send raw pane capture trimmed
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


# --- Phase 3 / B7 slash commands ---


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
        # Direct create. We mimic _create_and_activate_session without a
        # CallbackQuery — send a status message and create.
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
        del hook_ok  # advisory only
        sess = session_manager.create_session(
            name=name_arg or created_wname or "",
            window_id=created_wid,
            workdir=target_path,
        )
        ws = session_manager.get_window_state(created_wid)
        if ws.session_id:
            session_manager.set_session_claude_id(sess.id, ws.session_id)
        session_manager.set_active_session(user.id, sess.id)
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
    await safe_reply(update.message, msg_text, reply_markup=keyboard)


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/list` — show all live sessions with state and short usage."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    sessions = session_manager.list_user_sessions(
        user.id, states=("active", "idle", "lost")
    )
    if not sessions:
        await safe_reply(update.message, "No live sessions. Use /new to create one.")
        return

    active_sess = session_manager.get_active_session(user.id)
    active_id = active_sess.id if active_sess else ""

    lines = ["*Live sessions*", ""]
    for s in sessions:
        marker = "✓" if s.id == active_id else " "
        usage = (
            f"{s.token_usage_total // 1000}k tok"
            if s.token_usage_total
            else "0 tok"
        )
        wd = s.workdir or "?"
        lines.append(
            f"{marker} `{s.id}` *{s.name}* ({s.state}) — {usage}\n  {wd}"
        )
    keyboard = build_switcher_keyboard(user.id)
    sent = await safe_reply(update.message, "\n".join(lines), reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user.id, sent.message_id)


async def use_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/use <name-or-id>` — make the named session active."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await safe_reply(update.message, "Usage: `/use <name-or-id>`")
        return
    sess = _resolve_ident(args[1].strip())
    if sess is None:
        await safe_reply(update.message, "❌ Session not found.")
        return
    if sess.state == "archived" or sess.state == "completed":
        await safe_reply(
            update.message,
            f"❌ `{sess.name}` is archived. Use /archive to restore it.",
        )
        return
    if sess.state == "lost":
        await safe_reply(
            update.message,
            f"❌ `{sess.name}` is lost (tmux window vanished). Use /archive to restore.",
        )
        return
    session_manager.set_active_session(user.id, sess.id)
    preview = await _render_session_preview(sess)
    keyboard = build_switcher_keyboard(user.id)
    sent = await safe_reply(update.message, preview, reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user.id, sent.message_id)


async def rename_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/rename <old-name-or-id> <new-name>`."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split(maxsplit=2)
    if len(args) < 3:
        await safe_reply(update.message, "Usage: `/rename <old> <new-name>`")
        return
    sess = _resolve_ident(args[1].strip())
    if sess is None:
        await safe_reply(update.message, "❌ Session not found.")
        return
    new_name = args[2].strip()
    session_manager.rename_session(sess.id, new_name)
    if sess.window_id:
        await tmux_manager.rename_window(sess.window_id, new_name)
        session_manager.update_display_name(sess.window_id, new_name)
    await safe_reply(update.message, f"✅ Renamed to `{new_name}`")


async def _archive_session(
    user_id: int,
    bot: Bot,
    sess: Session,
    *,
    completed: bool,
) -> None:
    """Kill tmux window if alive and mark the Session archived/completed.

    Used by both /kill and /done. Cleans up any per-window in-memory state.
    """
    wid = sess.window_id
    if wid:
        w = await tmux_manager.find_window_by_id(wid)
        if w:
            await tmux_manager.kill_window(w.window_id)
        await clear_session_state(user_id, wid, bot)
    session_manager.mark_session_archived(sess.id, completed=completed)


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/kill <name-or-id>` — stop and archive immediately."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split(maxsplit=1)
    if len(args) < 2:
        await safe_reply(update.message, "Usage: `/kill <name-or-id>`")
        return
    sess = _resolve_ident(args[1].strip())
    if sess is None or sess.state not in ("active", "idle", "lost"):
        await safe_reply(update.message, "❌ Session not found or already archived.")
        return
    await _archive_session(user.id, context.bot, sess, completed=False)
    await safe_reply(update.message, f"✅ Killed `{sess.name}`")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/done [<name-or-id>]` — mark goal achieved and archive.

    Without an argument, applies to the user's active session.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    args = (update.message.text or "").split(maxsplit=1)
    if len(args) >= 2:
        sess = _resolve_ident(args[1].strip())
    else:
        sess = session_manager.get_active_session(user.id)
    if sess is None or sess.state not in ("active", "idle"):
        await safe_reply(update.message, "❌ Session not found or not live.")
        return
    await _archive_session(user.id, context.bot, sess, completed=True)
    await safe_reply(update.message, f"🎉 Marked `{sess.name}` as done.")


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/stop` — send Escape to the active session's tmux window."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = _active_window(user.id)
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


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/status` — usage breakdown (5h window, weekly, per-session)."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    usage = await compute_user_usage(user.id)
    text = format_usage_status(user.id, usage)
    await safe_reply(update.message, text)


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


# --- Screenshot keyboard with quick control keys ---

# key_id → (tmux_key, enter, literal)
_KEYS_SEND_MAP: dict[str, tuple[str, bool, bool]] = {
    "up": ("Up", False, False),
    "dn": ("Down", False, False),
    "lt": ("Left", False, False),
    "rt": ("Right", False, False),
    "esc": ("Escape", False, False),
    "ent": ("Enter", False, False),
    "spc": ("Space", False, False),
    "tab": ("Tab", False, False),
    "cc": ("C-c", False, False),
}

# key_id → display label (shown in callback answer toast)
_KEY_LABELS: dict[str, str] = {
    "up": "↑",
    "dn": "↓",
    "lt": "←",
    "rt": "→",
    "esc": "⎋ Esc",
    "ent": "⏎ Enter",
    "spc": "␣ Space",
    "tab": "⇥ Tab",
    "cc": "^C",
}


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for screenshot: control keys + refresh."""

    def btn(label: str, key_id: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(
            label,
            callback_data=f"{CB_KEYS_PREFIX}{key_id}:{window_id}"[:64],
        )

    return InlineKeyboardMarkup(
        [
            [btn("␣ Space", "spc"), btn("↑", "up"), btn("⇥ Tab", "tab")],
            [btn("←", "lt"), btn("↓", "dn"), btn("→", "rt")],
            [btn("⎋ Esc", "esc"), btn("^C", "cc"), btn("⏎ Enter", "ent")],
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"[:64],
                )
            ],
        ]
    )


async def forward_command_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Forward any non-bot command as a slash command to the active Claude session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    cmd_text = update.message.text or ""
    cc_slash = cmd_text.split("@")[0]  # strip bot mention
    wid = _active_window(user.id)
    if not wid:
        await safe_reply(update.message, "❌ No active session. Use /new to create one.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(update.message, f"❌ Window '{display}' no longer exists.")
        return

    display = session_manager.get_display_name(wid)
    logger.info(
        "Forwarding command %s to window %s (user=%d)", cc_slash, display, user.id
    )
    await update.message.chat.send_action(ChatAction.TYPING)
    success, message = await session_manager.send_to_window(wid, cc_slash)
    if success:
        await safe_reply(update.message, f"⚡ [{display}] Sent: {cc_slash}")
        # If /clear command was sent, clear the session association
        # so we can detect the new session after first message.
        if cc_slash.strip().lower() == "/clear":
            logger.info("Clearing session for window %s after /clear", display)
            session_manager.clear_window_session(wid)

        # Interactive commands (e.g. /model) render a terminal-based UI
        # with no JSONL tool_use entry. The status poller detects them.
    else:
        await safe_reply(update.message, f"❌ {message}")


async def unsupported_content_handler(
    update: Update,
    _context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Reply to non-text messages (stickers, video, etc.)."""
    if not update.message:
        return
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    logger.debug("Unsupported content from user %d", user.id)
    await safe_reply(
        update.message,
        "⚠ Only text, photo, and voice messages are supported. Stickers, video, and other media cannot be forwarded to Claude Code.",
    )


async def _forward_inbox_file(
    user_id: int,
    wid: str,
    chat_id: int,
    file_path: Path,
    caption: str,
    label: str,
    bot: Bot,
) -> tuple[bool, str]:
    """Send a synthetic 'received file' notice to the active session."""
    rel = file_path.name
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess else ""
    location = (
        f"{workdir}/.ccbot-inbox/{rel}" if workdir else str(file_path)
    )
    if caption:
        text_to_send = f"{caption}\n\n({label} attached: {location})"
    else:
        text_to_send = f"({label} attached: {location})"
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except Exception:
        pass
    clear_status_msg_info(user_id, wid)
    return await session_manager.send_to_window(wid, text_to_send)


async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photos sent by the user: drop into inbox, notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.photo:
        return

    wid = _active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()
    filename = f"{photo.file_unique_id}.jpg"

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    success, message = await _forward_inbox_file(
        user.id, wid, user.id, file_path, caption, "image", context.bot
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    await safe_reply(update.message, "📷 Image sent to Claude Code.")


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle generic documents: drop into inbox, notify Claude."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.document:
        return

    wid = _active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    doc = update.message.document
    sess = session_manager.find_session_by_window(wid)
    workdir = sess.workdir if sess and sess.workdir else str(ccbot_dir() / "images")
    filename = doc.file_name or f"{doc.file_unique_id}.bin"
    tg_file = await doc.get_file()

    async def _fetch(target: Path) -> None:
        await tg_file.download_to_drive(target)

    file_path = await save_inbox_file(workdir, filename, _fetch)

    caption = update.message.caption or ""
    success, message = await _forward_inbox_file(
        user.id, wid, user.id, file_path, caption, "document", context.bot
    )
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return
    await safe_reply(update.message, "📎 Document sent to Claude Code.")


async def voice_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle voice messages: transcribe via OpenAI and forward text to Claude Code."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.voice:
        return

    if config.voice_backend == "off":
        await safe_reply(update.message, "⚠ Voice is disabled (VOICE_BACKEND=off).")
        return
    if config.voice_backend == "openai" and not config.openai_api_key:
        await safe_reply(
            update.message,
            "⚠ VOICE_BACKEND=openai but OPENAI_API_KEY is unset.\n"
            "Set the key or switch VOICE_BACKEND to whisper/auto.",
        )
        return

    wid = _active_window(user.id)
    if wid is None:
        await safe_reply(
            update.message,
            "❌ No active session. Send a text message first or use /new.",
        )
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    # Download voice as in-memory bytes
    voice_file = await update.message.voice.get_file()
    ogg_data = bytes(await voice_file.download_as_bytearray())

    # Transcribe
    try:
        text = await transcribe_voice(ogg_data)
    except ValueError as e:
        await safe_reply(update.message, f"⚠ {e}")
        return
    except Exception as e:
        logger.error("Voice transcription failed: %s", e)
        await safe_reply(update.message, f"⚠ Transcription failed: {e}")
        return

    await update.message.chat.send_action(ChatAction.TYPING)
    clear_status_msg_info(user.id, wid)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    await safe_reply(update.message, f'🎤 "{text}"')


# Active bash capture tasks: (user_id, window_id) → asyncio.Task
_bash_capture_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}


def _cancel_bash_capture(user_id: int, window_id: str) -> None:
    """Cancel any running bash capture for this (user, window) pair."""
    key = (user_id, window_id)
    task = _bash_capture_tasks.pop(key, None)
    if task and not task.done():
        task.cancel()


async def _capture_bash_output(
    bot: Bot,
    user_id: int,
    window_id: str,
    command: str,
) -> None:
    """Background task: capture ``!`` bash command output from tmux pane.

    Sends the first captured output as a new message, then edits it
    in-place as more output appears.  Stops after 30 s or when cancelled
    (e.g. user sends a new message, which pushes content down).
    """
    try:
        # Wait for the command to start producing output
        await asyncio.sleep(2.0)

        chat_id = user_id  # DM mode
        msg_id: int | None = None
        last_output: str = ""

        for _ in range(30):
            raw = await tmux_manager.capture_pane(window_id)
            if raw is None:
                return

            output = extract_bash_output(raw, command)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Skip edit if nothing changed
            if output == last_output:
                await asyncio.sleep(1.0)
                continue

            last_output = output

            # Truncate to fit Telegram's 4096-char limit
            if len(output) > 3800:
                output = "… " + output[-3800:]

            if msg_id is None:
                # First capture — send a new message
                sent = await send_with_fallback(bot, chat_id, output)
                if sent:
                    msg_id = sent.message_id
            else:
                # Subsequent captures — edit in place
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=convert_markdown(output),
                        parse_mode="MarkdownV2",
                        link_preview_options=NO_LINK_PREVIEW,
                    )
                except Exception:
                    try:
                        await bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=msg_id,
                            text=output,
                            link_preview_options=NO_LINK_PREVIEW,
                        )
                    except Exception:
                        pass

            await asyncio.sleep(1.0)
    except asyncio.CancelledError:
        return
    finally:
        _bash_capture_tasks.pop((user_id, window_id), None)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        if update.message:
            await safe_reply(update.message, "You are not authorized to use this bot.")
        return

    if not update.message or not update.message.text:
        return

    text = update.message.text

    # Ignore text while a picker UI is mid-flight (any picker state).
    state = context.user_data.get(STATE_KEY) if context.user_data else None
    if state in (
        STATE_SELECTING_WINDOW,
        STATE_BROWSING_DIRECTORY,
        STATE_SELECTING_SESSION,
    ):
        await safe_reply(
            update.message,
            "Please use the picker above, or tap Cancel.",
        )
        return

    # Reply-quote routing (A4): if the user replied to a bot message that
    # belongs to a non-active session, send this single message there
    # without changing the active session pointer.
    reply = update.message.reply_to_message
    if reply is not None:
        target_sid = lookup_session_for_message(user.id, reply.message_id)
        if target_sid:
            target = session_manager.get_session(target_sid)
            active_sess = session_manager.get_active_session(user.id)
            same_as_active = active_sess is not None and active_sess.id == target_sid
            if (
                target is not None
                and target.window_id
                and target.state in ("active", "idle")
                and not same_as_active
            ):
                tw = await tmux_manager.find_window_by_id(target.window_id)
                if tw:
                    ok, sm = await session_manager.send_to_window(
                        target.window_id, text
                    )
                    if ok:
                        session_manager.touch_session(target.id)
                        await safe_reply(
                            update.message,
                            f"→ \\[{target.name or target.id}\\]",
                        )
                        return
                    await safe_reply(update.message, f"❌ {sm}")
                    return

    wid = _active_window(user.id)
    if wid is None:
        # No active session — start a directory browser to create one.
        # The pending text is held in user_data and forwarded after creation.
        logger.info(
            "No active session: showing directory browser (user=%d)",
            user.id,
        )
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
            context.user_data["_pending_text"] = text
        await safe_reply(update.message, msg_text, reply_markup=keyboard)
        return

    # Active session exists — forward text to its window.
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        display = session_manager.get_display_name(wid)
        logger.info(
            "Stale active session: window %s gone (user=%d)",
            display,
            user.id,
        )
        sess = session_manager.find_session_by_window(wid)
        if sess is not None:
            session_manager.mark_session_lost(sess.id)
        await clear_session_state(user.id, wid, context.bot)
        await safe_reply(
            update.message,
            f"❌ Window '{display}' no longer exists.\n"
            "Send a message to start a new session.",
        )
        return

    await update.message.chat.send_action(ChatAction.TYPING)

    # Cancel any running bash capture — new message pushes pane content down
    _cancel_bash_capture(user.id, wid)

    # Check for pending interactive UI before sending text.
    # This catches UIs (permission prompts, etc.) that status polling might have missed.
    pane_text = await tmux_manager.capture_pane(w.window_id)
    if pane_text and is_interactive_ui(pane_text):
        logger.info(
            "Detected pending interactive UI before sending text (user=%d, window=%s)",
            user.id,
            wid,
        )
        await handle_interactive_ui(context.bot, user.id, wid)
        await asyncio.sleep(0.3)

    success, message = await session_manager.send_to_window(wid, text)
    if not success:
        await safe_reply(update.message, f"❌ {message}")
        return

    # Touch session activity for idle TTL and trigger H6 auto-naming on
    # the first/second meaningful user message.
    sess = session_manager.find_session_by_window(wid)
    if sess is not None:
        session_manager.touch_session(sess.id)
        # Trigger auto-name on a sufficiently-long first message.
        looks_default = (not sess.name) or sess.name.startswith("session-")
        if looks_default and len(text) >= 50:
            asyncio.create_task(maybe_auto_name(sess.id, text))

    # Start background capture for ! bash command output
    if text.startswith("!") and len(text) > 1:
        bash_cmd = text[1:]  # strip leading "!"
        task = asyncio.create_task(
            _capture_bash_output(context.bot, user.id, wid, bash_cmd)
        )
        _bash_capture_tasks[(user.id, wid)] = task

    # If in interactive mode, refresh the UI after sending text
    interactive_window = get_interactive_window(user.id)
    if interactive_window and interactive_window == wid:
        await asyncio.sleep(0.2)
        await handle_interactive_ui(context.bot, user.id, wid)


# --- Window creation helper ---


async def _create_and_activate_session(
    query: object,
    context: ContextTypes.DEFAULT_TYPE,
    user: object,
    selected_path: str,
    resume_session_id: str | None = None,
) -> None:
    """Create a tmux window, register a new Session, make it active, forward pending text.

    Shared by CB_DIR_CONFIRM (no sessions), CB_SESSION_NEW, and CB_SESSION_SELECT.
    """
    from telegram import CallbackQuery, User

    assert isinstance(query, CallbackQuery)
    assert isinstance(user, User)

    success, message, created_wname, created_wid = await tmux_manager.create_window(
        selected_path, resume_session_id=resume_session_id
    )
    if not success:
        await safe_edit(query, f"❌ {message}")
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        await query.answer("Failed")
        return

    logger.info(
        "Window created: %s (id=%s) at %s (user=%d, resume=%s)",
        created_wname,
        created_wid,
        selected_path,
        user.id,
        resume_session_id,
    )
    # Wait for the SessionStart hook to register in session_map.
    # Resume sessions need a longer timeout (loading state).
    hook_timeout = 15.0 if resume_session_id else 5.0
    hook_ok = await session_manager.wait_for_session_map_entry(
        created_wid, timeout=hook_timeout
    )

    # `claude --resume` records a new session_id in the hook, but messages still
    # write to the resumed JSONL. Override window_state to track the original.
    if resume_session_id:
        ws = session_manager.get_window_state(created_wid)
        if not hook_ok:
            logger.warning(
                "Hook timed out for resume window %s, "
                "manually setting session_id=%s cwd=%s",
                created_wid,
                resume_session_id,
                selected_path,
            )
            ws.session_id = resume_session_id
            ws.cwd = str(selected_path)
            ws.window_name = created_wname
            session_manager._save_state()
        elif ws.session_id != resume_session_id:
            logger.info(
                "Resume override: window %s session_id %s -> %s",
                created_wid,
                ws.session_id,
                resume_session_id,
            )
            ws.session_id = resume_session_id
            session_manager._save_state()

    # Register Session record and make it active. Honor /new <name> if any.
    pending_name = (
        context.user_data.pop("_pending_session_name", "") if context.user_data else ""
    )
    sess = session_manager.create_session(
        name=pending_name or created_wname or "",
        window_id=created_wid,
        workdir=selected_path,
    )
    # If we have a claude session id from the hook or the resume target, store it.
    ws = session_manager.get_window_state(created_wid)
    if ws.session_id:
        session_manager.set_session_claude_id(sess.id, ws.session_id)
    session_manager.set_active_session(user.id, sess.id)

    status = "Resumed" if resume_session_id else "Created"
    await safe_edit(
        query,
        f"✅ {message}\n\n{status}. Send messages here.",
    )

    # Forward any pending text held while the picker was up.
    pending_text = (
        context.user_data.get("_pending_text") if context.user_data else None
    )
    if pending_text:
        logger.debug(
            "Forwarding pending text to window %s (len=%d)",
            created_wname,
            len(pending_text),
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        send_ok, send_msg = await session_manager.send_to_window(
            created_wid, pending_text
        )
        if not send_ok:
            logger.warning("Failed to forward pending text: %s", send_msg)
            await safe_send(
                context.bot,
                user.id,
                f"❌ Failed to send pending message: {send_msg}",
            )
    await query.answer("Created")


# --- Callback query handler ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        await query.answer("Not authorized")
        return

    data = query.data

    # History: older/newer pagination
    # Format: hp:<page>:<window_id>:<start>:<end> or hn:<page>:<window_id>:<start>:<end>
    if data.startswith(CB_HISTORY_PREV) or data.startswith(CB_HISTORY_NEXT):
        prefix_len = len(CB_HISTORY_PREV)  # same length for both
        rest = data[prefix_len:]
        try:
            parts = rest.split(":")
            if len(parts) < 4:
                # Old format without byte range: page:window_id
                offset_str, window_id = rest.split(":", 1)
                start_byte, end_byte = 0, 0
            else:
                # New format: page:window_id:start:end (window_id may contain colons)
                offset_str = parts[0]
                start_byte = int(parts[-2])
                end_byte = int(parts[-1])
                window_id = ":".join(parts[1:-2])
            offset = int(offset_str)
        except (ValueError, IndexError):
            await query.answer("Invalid data")
            return

        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await send_history(
                query,
                window_id,
                offset=offset,
                edit=True,
                start_byte=start_byte,
                end_byte=end_byte,
                # Don't pass user_id for pagination - offset update only on initial view
                # This prevents offset from going backwards if new messages arrive while paging
            )
        else:
            await safe_edit(query, "Window no longer exists.")
        await query.answer("Page updated")

    # Directory browser handlers
    elif data.startswith(CB_DIR_SELECT):
        # callback_data contains index, not dir name (to avoid 64-byte limit)
        try:
            idx = int(data[len(CB_DIR_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        # Look up dir name from cached subdirs
        cached_dirs: list[str] = (
            context.user_data.get(BROWSE_DIRS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_dirs):
            await query.answer(
                "Directory list changed, please refresh", show_alert=True
            )
            return
        subdir_name = cached_dirs[idx]

        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        new_path = (Path(current_path) / subdir_name).resolve()

        if not new_path.exists() or not new_path.is_dir():
            await query.answer("Directory not found", show_alert=True)
            return

        new_path_str = str(new_path)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = new_path_str
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(new_path_str)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_UP:
        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        current = Path(current_path).resolve()
        parent = current.parent
        # No restriction - allow navigating anywhere

        parent_path = str(parent)
        if context.user_data is not None:
            context.user_data[BROWSE_PATH_KEY] = parent_path
            context.user_data[BROWSE_PAGE_KEY] = 0

        msg_text, keyboard, subdirs = build_directory_browser(parent_path)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data.startswith(CB_DIR_PAGE):
        try:
            pg = int(data[len(CB_DIR_PAGE) :])
        except ValueError:
            await query.answer("Invalid data")
            return
        default_path = str(Path.home())
        current_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        if context.user_data is not None:
            context.user_data[BROWSE_PAGE_KEY] = pg

        msg_text, keyboard, subdirs = build_directory_browser(current_path, pg)
        if context.user_data is not None:
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    elif data == CB_DIR_CONFIRM:
        default_path = str(Path.home())
        selected_path = (
            context.user_data.get(BROWSE_PATH_KEY, default_path)
            if context.user_data
            else default_path
        )
        clear_browse_state(context.user_data)

        # Check for existing sessions in this directory
        sessions = await session_manager.list_sessions_for_directory(selected_path)
        if sessions:
            # Show session picker — store state for later
            if context.user_data is not None:
                context.user_data[STATE_KEY] = STATE_SELECTING_SESSION
                context.user_data[SESSIONS_KEY] = sessions
                context.user_data["_selected_path"] = selected_path
            text, keyboard = build_session_picker(sessions)
            await safe_edit(query, text, reply_markup=keyboard)
            await query.answer()
            return

        # No existing sessions — create new window directly
        await _create_and_activate_session(
            query, context, user, selected_path
        )

    elif data == CB_DIR_CANCEL:
        clear_browse_state(context.user_data)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Session picker: resume existing session
    elif data.startswith(CB_SESSION_SELECT):
        try:
            idx = int(data[len(CB_SESSION_SELECT) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_sessions = (
            context.user_data.get(SESSIONS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_sessions):
            await query.answer("Session not found")
            return

        session = cached_sessions[idx]
        selected_path = (
            context.user_data.get("_selected_path", str(Path.home()))
            if context.user_data
            else str(Path.home())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_activate_session(
            query, context, user, selected_path,
            resume_session_id=session.session_id,
        )

    elif data == CB_SESSION_NEW:
        selected_path = (
            context.user_data.get("_selected_path", str(Path.home()))
            if context.user_data
            else str(Path.home())
        )
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)

        await _create_and_activate_session(query, context, user, selected_path)

    elif data == CB_SESSION_CANCEL:
        clear_session_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_selected_path", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Window picker: bind existing window
    elif data.startswith(CB_WIN_BIND):
        try:
            idx = int(data[len(CB_WIN_BIND) :])
        except ValueError:
            await query.answer("Invalid data")
            return

        cached_windows: list[str] = (
            context.user_data.get(UNBOUND_WINDOWS_KEY, []) if context.user_data else []
        )
        if idx < 0 or idx >= len(cached_windows):
            await query.answer("Window list changed, please retry", show_alert=True)
            return
        selected_wid = cached_windows[idx]

        # Verify window still exists
        w = await tmux_manager.find_window_by_id(selected_wid)
        if not w:
            display = session_manager.get_display_name(selected_wid)
            await query.answer(f"Window '{display}' no longer exists", show_alert=True)
            return

        display = w.window_name
        clear_window_picker_state(context.user_data)

        # Adopt the unbound window into a fresh Session record and activate it.
        sess = session_manager.find_session_by_window(selected_wid)
        if sess is None:
            ws = session_manager.get_window_state(selected_wid)
            sess = session_manager.create_session(
                name=display,
                window_id=selected_wid,
                workdir=ws.cwd or "",
            )
            if ws.session_id:
                session_manager.set_session_claude_id(sess.id, ws.session_id)
        else:
            session_manager.set_session_window(sess.id, selected_wid)
        session_manager.set_active_session(user.id, sess.id)

        await safe_edit(query, f"✅ Bound to window `{display}`")

        # Forward pending text if any
        pending_text = (
            context.user_data.get("_pending_text") if context.user_data else None
        )
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        if pending_text:
            send_ok, send_msg = await session_manager.send_to_window(
                selected_wid, pending_text
            )
            if not send_ok:
                logger.warning("Failed to forward pending text: %s", send_msg)
                await safe_send(
                    context.bot,
                    user.id,
                    f"❌ Failed to send pending message: {send_msg}",
                )
        await query.answer("Bound")

    # Window picker: new session → transition to directory browser
    elif data == CB_WIN_NEW:
        clear_window_picker_state(context.user_data)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query, msg_text, reply_markup=keyboard)
        await query.answer()

    # Window picker: cancel
    elif data == CB_WIN_CANCEL:
        clear_window_picker_state(context.user_data)
        if context.user_data is not None:
            context.user_data.pop("_pending_text", None)
        await safe_edit(query, "Cancelled")
        await query.answer("Cancelled")

    # Screenshot: Refresh
    elif data.startswith(CB_SCREENSHOT_REFRESH):
        window_id = data[len(CB_SCREENSHOT_REFRESH) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window no longer exists", show_alert=True)
            return

        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if not text:
            await query.answer("Failed to capture pane", show_alert=True)
            return

        png_bytes = await text_to_image(text, with_ansi=True)
        keyboard = _build_screenshot_keyboard(window_id)
        try:
            await query.edit_message_media(
                media=InputMediaDocument(
                    media=io.BytesIO(png_bytes), filename="screenshot.png"
                ),
                reply_markup=keyboard,
            )
            await query.answer("Refreshed")
        except Exception as e:
            logger.error(f"Failed to refresh screenshot: {e}")
            await query.answer("Failed to refresh", show_alert=True)

    elif data == "noop":
        await query.answer()

    # A8 switcher: tap on already-active button — no-op feedback.
    elif data == CB_SW_NOOP:
        await query.answer("already active")

    # A8 switcher: switch active session to <session.id>
    elif data.startswith(CB_SW_USE):
        target_id = data[len(CB_SW_USE) :]
        sess = session_manager.get_session(target_id)
        if sess is None or sess.state not in ("active", "idle"):
            await query.answer("Session not available", show_alert=True)
            return
        session_manager.set_active_session(user.id, target_id)

        # Render the context preview into the same message and re-attach
        # the switcher so the active marker moves to the new session.
        try:
            preview = await _render_session_preview(sess)
            await query.edit_message_text(text=preview)
        except Exception as e:
            logger.debug("preview edit_message_text failed: %s", e)
        keyboard = build_switcher_keyboard(user.id)
        if keyboard is not None:
            try:
                await query.edit_message_reply_markup(reply_markup=keyboard)
            except Exception as e:
                logger.debug("preview reply markup failed: %s", e)
            else:
                if query.message:
                    session_manager.set_last_switcher_msg(
                        user.id, query.message.message_id
                    )
        await query.answer(f"→ {sess.name or sess.id}")

    # Archive: pagination
    elif data.startswith(CB_ARC_PAGE):
        try:
            page = int(data[len(CB_ARC_PAGE) :])
        except ValueError:
            await query.answer("Invalid page")
            return
        show_all = bool(
            context.user_data and context.user_data.get("_arc_show_all", False)
        )
        lookback = (
            config.archive_purge_after if show_all else DEFAULT_LOOKBACK_SECONDS
        )
        text, keyboard = build_archive_page(
            page=page, lookback_seconds=lookback, show_all=show_all
        )
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("archive page edit failed: %s", e)
        await query.answer()

    # Archive: toggle 0-72h vs 0-14d
    elif data == CB_ARC_ALL:
        cur = bool(
            context.user_data and context.user_data.get("_arc_show_all", False)
        )
        new = not cur
        if context.user_data is not None:
            context.user_data["_arc_show_all"] = new
        lookback = config.archive_purge_after if new else DEFAULT_LOOKBACK_SECONDS
        text, keyboard = build_archive_page(
            page=0, lookback_seconds=lookback, show_all=new
        )
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("archive toggle edit failed: %s", e)
        await query.answer("→ 14d" if new else "→ 72h")

    # Archive: restore <session.id>
    elif data.startswith(CB_ARC_RESTORE):
        sid = data[len(CB_ARC_RESTORE) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer("Session not found", show_alert=True)
            return
        ok, msg = await restore_session(context.bot, user.id, sess)
        if ok:
            preview = await _render_session_preview(sess)
            keyboard = build_switcher_keyboard(user.id)
            try:
                await query.edit_message_text(text=preview, reply_markup=keyboard)
            except Exception as e:
                logger.debug("archive restore edit failed: %s", e)
            else:
                if query.message and keyboard is not None:
                    session_manager.set_last_switcher_msg(
                        user.id, query.message.message_id
                    )
            await query.answer("Restored")
        else:
            await query.answer(f"Restore failed: {msg}", show_alert=True)

    # Archive: inspect <session.id>
    elif data.startswith(CB_ARC_INSPECT):
        sid = data[len(CB_ARC_INSPECT) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer("Session not found", show_alert=True)
            return
        preview = await _render_session_preview(sess)
        from telegram import (
            InlineKeyboardButton as _IKB,
            InlineKeyboardMarkup as _IKM,
        )

        kb = _IKM(
            [
                [
                    _IKB(
                        "⤴ Restore",
                        callback_data=f"{CB_ARC_RESTORE}{sess.id}"[:64],
                    ),
                    _IKB(
                        "🗑 Delete",
                        callback_data=f"{CB_ARC_DELETE}{sess.id}"[:64],
                    ),
                    _IKB("◀ Back", callback_data=CB_ARC_BACK),
                ]
            ]
        )
        try:
            await query.edit_message_text(text=preview, reply_markup=kb)
        except Exception as e:
            logger.debug("archive inspect edit failed: %s", e)
        await query.answer()

    # Archive: back from inspect to listing
    elif data == CB_ARC_BACK:
        show_all = bool(
            context.user_data and context.user_data.get("_arc_show_all", False)
        )
        lookback = (
            config.archive_purge_after if show_all else DEFAULT_LOOKBACK_SECONDS
        )
        text, keyboard = build_archive_page(
            page=0, lookback_seconds=lookback, show_all=show_all
        )
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("archive back edit failed: %s", e)
        await query.answer()

    # Archive: delete <session.id>
    elif data.startswith(CB_ARC_DELETE):
        sid = data[len(CB_ARC_DELETE) :]
        if session_manager.delete_session(sid):
            show_all = bool(
                context.user_data and context.user_data.get("_arc_show_all", False)
            )
            lookback = (
                config.archive_purge_after if show_all else DEFAULT_LOOKBACK_SECONDS
            )
            text, keyboard = build_archive_page(
                page=0, lookback_seconds=lookback, show_all=show_all
            )
            try:
                await query.edit_message_text(text=text, reply_markup=keyboard)
            except Exception as e:
                logger.debug("archive delete edit failed: %s", e)
            await query.answer("Deleted")
        else:
            await query.answer("Already gone", show_alert=True)

    # A8 switcher: + new — open directory browser
    elif data == CB_SW_NEW:
        clear_browse_state(context.user_data)
        clear_window_picker_state(context.user_data)
        clear_session_picker_state(context.user_data)
        start_path = str(Path.home())
        msg_text, keyboard, subdirs = build_directory_browser(start_path)
        if context.user_data is not None:
            context.user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
            context.user_data[BROWSE_PATH_KEY] = start_path
            context.user_data[BROWSE_PAGE_KEY] = 0
            context.user_data[BROWSE_DIRS_KEY] = subdirs
        try:
            await query.edit_message_text(text=msg_text, reply_markup=keyboard)
        except Exception:
            await safe_send(context.bot, user.id, msg_text, reply_markup=keyboard)
        await query.answer()

    # Interactive UI: Up arrow
    elif data.startswith(CB_ASK_UP):
        window_id = data[len(CB_ASK_UP) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Up", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer()

    # Interactive UI: Down arrow
    elif data.startswith(CB_ASK_DOWN):
        window_id = data[len(CB_ASK_DOWN) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Down", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer()

    # Interactive UI: Left arrow
    elif data.startswith(CB_ASK_LEFT):
        window_id = data[len(CB_ASK_LEFT) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Left", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer()

    # Interactive UI: Right arrow
    elif data.startswith(CB_ASK_RIGHT):
        window_id = data[len(CB_ASK_RIGHT) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Right", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer()

    # Interactive UI: Escape
    elif data.startswith(CB_ASK_ESC):
        window_id = data[len(CB_ASK_ESC) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Escape", enter=False, literal=False
            )
            await clear_interactive_msg(user.id, context.bot, window_id)
        await query.answer("⎋ Esc")

    # Interactive UI: Enter
    elif data.startswith(CB_ASK_ENTER):
        window_id = data[len(CB_ASK_ENTER) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Enter", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer("⏎ Enter")

    # Interactive UI: Space
    elif data.startswith(CB_ASK_SPACE):
        window_id = data[len(CB_ASK_SPACE) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(
                w.window_id, "Space", enter=False, literal=False
            )
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer("␣ Space")

    # Interactive UI: Tab
    elif data.startswith(CB_ASK_TAB):
        window_id = data[len(CB_ASK_TAB) :]
        w = await tmux_manager.find_window_by_id(window_id)
        if w:
            await tmux_manager.send_keys(w.window_id, "Tab", enter=False, literal=False)
            await asyncio.sleep(0.5)
            await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer("⇥ Tab")

    # Interactive UI: refresh display
    elif data.startswith(CB_ASK_REFRESH):
        window_id = data[len(CB_ASK_REFRESH) :]
        await handle_interactive_ui(context.bot, user.id, window_id)
        await query.answer("🔄")

    # Screenshot quick keys: send key to tmux window
    elif data.startswith(CB_KEYS_PREFIX):
        rest = data[len(CB_KEYS_PREFIX) :]
        colon_idx = rest.find(":")
        if colon_idx < 0:
            await query.answer("Invalid data")
            return
        key_id = rest[:colon_idx]
        window_id = rest[colon_idx + 1 :]

        key_info = _KEYS_SEND_MAP.get(key_id)
        if not key_info:
            await query.answer("Unknown key")
            return

        tmux_key, enter, literal = key_info
        w = await tmux_manager.find_window_by_id(window_id)
        if not w:
            await query.answer("Window not found", show_alert=True)
            return

        await tmux_manager.send_keys(
            w.window_id, tmux_key, enter=enter, literal=literal
        )
        await query.answer(_KEY_LABELS.get(key_id, key_id))

        # Refresh screenshot after key press
        await asyncio.sleep(0.5)
        text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
        if text:
            png_bytes = await text_to_image(text, with_ansi=True)
            keyboard = _build_screenshot_keyboard(window_id)
            try:
                await query.edit_message_media(
                    media=InputMediaDocument(
                        media=io.BytesIO(png_bytes),
                        filename="screenshot.png",
                    ),
                    reply_markup=keyboard,
                )
            except Exception:
                pass  # Screenshot unchanged or message too old


# --- Streaming response / notifications ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Handle a new assistant message — enqueue for sequential processing.

    Messages are queued per-user to ensure status messages always appear last.
    Routes via thread_bindings to deliver to the correct topic.
    """
    status = "complete" if msg.is_complete else "streaming"
    logger.info(
        f"handle_new_message [{status}]: session={msg.session_id}, "
        f"text_len={len(msg.text)}"
    )

    # DM mode: route to every Session whose claude_session_id matches —
    # whether it's currently the user's active session or a background session.
    # Background sessions still emit notifications via their own "live cards".
    targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)

    if not targets:
        # Try to bind via the session_map (claude_session_id -> window_id) if a
        # Session record exists with the matching window but no claude_session_id yet.
        await session_manager.load_session_map()
        targets = session_manager.all_user_sessions_with_claude_id(msg.session_id)
    if not targets:
        logger.info("No session record for claude session %s", msg.session_id)
        return

    for user_id, sess in targets:
        wid = sess.window_id
        if not wid:
            continue
        # Touch session activity for idle TTL.
        session_manager.touch_session(sess.id)

        # Skip tool-call notifications when display config disables them.
        if not config.show_tool_calls and msg.content_type in (
            "tool_use",
            "tool_result",
        ):
            continue

        # Handle interactive tools specially — capture terminal and send a
        # separate UI message with inline keyboard. (User-approved trigger.)
        if msg.tool_name in INTERACTIVE_TOOL_NAMES and msg.content_type == "tool_use":
            set_interactive_mode(user_id, wid)
            queue = get_message_queue(user_id)
            if queue:
                await queue.join()
            await asyncio.sleep(0.3)
            handled = await handle_interactive_ui(bot, user_id, wid)
            if handled:
                await push_event(
                    bot, user_id, sess, text=f"interactive prompt: {msg.tool_name}"
                )
                claude_sess = await session_manager.resolve_session_for_window(wid)
                if claude_sess and claude_sess.file_path:
                    try:
                        file_size = Path(claude_sess.file_path).stat().st_size
                        session_manager.update_user_window_offset(
                            user_id, wid, file_size
                        )
                    except OSError:
                        pass
                continue
            clear_interactive_mode(user_id)

        # Any non-interactive event for this window means the prior interactive
        # UI is no longer relevant — drop it from chat.
        if get_interactive_msg_id(user_id, wid):
            await clear_interactive_msg(user_id, bot, wid)

        # Single live card per session (active and background alike).
        # Streaming chunks update the card in place; only msg.is_complete
        # events with role=assistant content_type=text trigger finalization.
        if msg.is_complete:
            if msg.role == "assistant" and msg.content_type == "text":
                # Task complete: append final text, finalize, push.
                await finalize_task(bot, user_id, sess, msg.text or "")
            else:
                # Tool result (or other complete non-text events) — append to card.
                await update_session_card(bot, user_id, sess, msg)

            claude_sess = await session_manager.resolve_session_for_window(wid)
            if claude_sess and claude_sess.file_path:
                try:
                    file_size = Path(claude_sess.file_path).stat().st_size
                    session_manager.update_user_window_offset(user_id, wid, file_size)
                except OSError:
                    pass

            # G6 soft quota crossing — separate push, not card-merged.
            if msg.role == "assistant" and msg.content_type == "text":
                try:
                    su = await aggregate_session(sess)
                    sess.token_usage_total = su.tokens_total
                    if should_warn_quota(su):
                        pct = (
                            su.tokens_5h
                            * 100
                            // max(1, config.session_token_budget_5h)
                        )
                        await push_event(
                            bot,
                            user_id,
                            sess,
                            text=(
                                f"⚠ burned {pct}% of 5h quota "
                                f"({config.session_token_budget_5h // 1000}k)"
                            ),
                        )
                except Exception as e:
                    logger.debug("quota check failed: %s", e)
        else:
            # Streaming partial chunks — best-effort card update.
            await update_session_card(bot, user_id, sess, msg)



# --- App lifecycle ---


async def post_init(application: Application) -> None:
    global session_monitor, _status_poll_task

    await application.bot.delete_my_commands()

    # Order: session ops first, then prominent Claude switches (model/effort),
    # then everything else. /use and /rename are intentionally NOT in the menu —
    # the inline switcher covers their job (TODO: remove handlers in v1.5).
    bot_commands = [
        BotCommand("new", "Create a new session"),
        BotCommand("list", "List active sessions"),
    ]
    # Promote model + effort right after /new and /list per user preference.
    for cmd_name in ("model", "effort"):
        if cmd_name in CC_COMMANDS:
            bot_commands.append(BotCommand(cmd_name, CC_COMMANDS[cmd_name]))
    bot_commands.extend(
        [
            BotCommand("done", "Mark a session as done"),
            BotCommand("kill", "Stop and archive a session: /kill <name-or-id>"),
            BotCommand("stop", "Send Escape to interrupt the active session"),
            BotCommand("archive", "Browse archived sessions"),
            BotCommand("status", "Usage breakdown: 5h, weekly, per-session"),
            BotCommand("history", "Message history for the active session"),
            BotCommand("screenshot", "Terminal screenshot with control keys"),
            BotCommand("usage", "Show Claude Code usage remaining"),
        ]
    )
    # Append remaining Claude Code slash commands (excluding the promoted ones).
    for cmd_name, desc in CC_COMMANDS.items():
        if cmd_name in ("model", "effort"):
            continue
        bot_commands.append(BotCommand(cmd_name, desc))

    await application.bot.set_my_commands(bot_commands)

    # Re-resolve stale window IDs from persisted state against live tmux windows
    await session_manager.resolve_stale_ids()
    # DM mode: cross-check Session records against live tmux. Sessions whose
    # window vanished get state=lost and surface in /list with a Restore button
    # via /archive (or are restorable from /archive --all).
    await session_manager.reconcile_sessions_with_tmux()

    # Pre-fill global rate limiter bucket on restart.
    # AsyncLimiter starts at _level=0 (full burst capacity), but Telegram's
    # server-side counter persists across bot restarts.  Setting _level=max_rate
    # forces the bucket to start "full" so capacity drains in naturally (~1s).
    # AIORateLimiter has no per-private-chat limiter, so max_retries is the
    # primary protection (retry + pause all concurrent requests on 429).
    rate_limiter = application.bot.rate_limiter
    if rate_limiter and rate_limiter._base_limiter:
        rate_limiter._base_limiter._level = rate_limiter._base_limiter.max_rate
        logger.info("Pre-filled global rate limiter bucket")

    monitor = SessionMonitor()

    async def message_callback(msg: NewMessage) -> None:
        await handle_new_message(msg, application.bot)

    monitor.set_message_callback(message_callback)
    monitor.start()
    session_monitor = monitor
    logger.info("Session monitor started")

    # Start status polling task
    _status_poll_task = asyncio.create_task(status_poll_loop(application.bot))
    logger.info("Status polling task started")


async def post_shutdown(application: Application) -> None:
    global _status_poll_task

    # Stop status polling
    if _status_poll_task:
        _status_poll_task.cancel()
        try:
            await _status_poll_task
        except asyncio.CancelledError:
            pass
        _status_poll_task = None
        logger.info("Status polling stopped")

    # Stop all queue workers
    await shutdown_workers()

    if session_monitor:
        session_monitor.stop()
        logger.info("Session monitor stopped")

    await close_transcribe_client()


def create_bot() -> Application:
    builder = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if config.tg_proxy_url:
        # Route both the long-poll request and the regular bot API through the
        # configured proxy. Required when api.telegram.org is unreachable from
        # the host (e.g. RU-blocked IPs); see scripts/com.ccbot.plist comments.
        from telegram.request import HTTPXRequest

        builder = builder.request(
            HTTPXRequest(proxy=config.tg_proxy_url)
        ).get_updates_request(
            HTTPXRequest(proxy=config.tg_proxy_url)
        )
        logger.info("TG proxy enabled: %s", config.tg_proxy_url)
    application = builder.build()

    # Visible menu commands.
    application.add_handler(CommandHandler("history", history_command))
    application.add_handler(CommandHandler("screenshot", screenshot_command))
    application.add_handler(CommandHandler("usage", usage_command))
    application.add_handler(CommandHandler("new", new_command))
    application.add_handler(CommandHandler("list", list_command))
    application.add_handler(CommandHandler("kill", kill_command))
    application.add_handler(CommandHandler("done", done_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("archive", archive_command))
    application.add_handler(CommandHandler("status", status_command))
    # Hidden but functional — the inline switcher covers their use case.
    # TODO(v1.5): drop these handlers and the corresponding command bodies.
    application.add_handler(CommandHandler("use", use_command))
    application.add_handler(CommandHandler("rename", rename_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    # Forward any other /command to Claude Code
    application.add_handler(MessageHandler(filters.COMMAND, forward_command_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    # Photos: download and forward file path to Claude Code
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))
    # Documents: drop into the active session's inbox and notify
    application.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    # Voice: transcribe (whisper.cpp / Apple / OpenAI) and forward text
    application.add_handler(MessageHandler(filters.VOICE, voice_handler))
    # Catch-all: non-text content (stickers, video, etc.)
    application.add_handler(
        MessageHandler(
            ~filters.COMMAND & ~filters.TEXT & ~filters.StatusUpdate.ALL,
            unsupported_content_handler,
        )
    )

    return application
