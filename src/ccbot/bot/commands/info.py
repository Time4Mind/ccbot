"""Read-only info commands: /status, /history, /screenshot, /usage, /health.

These also expose ``emit_*`` helpers used by the inline Menu callbacks
when the user opens the same view from a button instead of a slash
command. ``build_screenshot_keyboard`` lives here because both the
/screenshot command and the CB_KEYS_* / CB_SCREENSHOT_REFRESH callback
paths need it.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...handlers.callback_data import CB_FT_MORE, CB_KEYS_PREFIX, CB_SCREENSHOT_REFRESH
from ...handlers.history import send_history
from ...handlers.message_sender import safe_reply, safe_send
from ...handlers.switcher import build_switcher_keyboard
from ...i18n import t
from ...screenshot import text_to_image
from ...session import session_manager
from ...tmux_manager import tmux_manager
from ...usage import compute_user_usage, format_usage_status
from .._common import active_window, is_user_allowed
from .lifecycle import build_live_sessions_text

logger = logging.getLogger(__name__)


# --- /history + emitter ---


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show message history for the active session."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = active_window(user.id)
    if not wid:
        await safe_reply(
            update.message, "❌ No active session. Use /new to create one."
        )
        return

    await send_history(update.message, wid)


async def emit_history(bot: Bot, user_id: int) -> None:
    """Render history as a fresh message (used by the Menu→History callback)."""
    wid = active_window(user_id)
    if not wid:
        await safe_send(bot, user_id, "❌ No active session.")
        return
    await send_history(target=None, window_id=wid, bot=bot, user_id=user_id)


# --- /screenshot + helpers ---


def build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Inline keyboard for screenshot: control keys + refresh."""

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


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Capture the active tmux pane and send it as an image."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = active_window(user.id)
    if not wid:
        await safe_reply(
            update.message, "❌ No active session. Use /new to create one."
        )
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
    keyboard = build_screenshot_keyboard(wid)
    await update.message.reply_document(
        document=io.BytesIO(png_bytes),
        filename="screenshot.png",
        reply_markup=keyboard,
    )


async def emit_screenshot_compact(query: Any, bot: Bot, user_id: int) -> None:
    """Delete the carrier message, reply with a compressed photo preview.

    Visually replaces the source menu in the chat. The photo carries a
    single ≡ Menu button; tapping it removes the photo and re-opens Menu.
    """
    wid = active_window(user_id)
    if not wid:
        await safe_send(bot, user_id, t(user_id, "toast.no_session"))
        return
    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_send(bot, user_id, t(user_id, "toast.window_gone"))
        return
    text = await tmux_manager.capture_pane(w.window_id, with_ansi=True)
    if not text:
        await safe_send(bot, user_id, "❌ Failed to capture pane content.")
        return
    png_bytes = await text_to_image(text, with_ansi=True)

    if query and query.message:
        try:
            await query.message.delete()
        except Exception as e:
            logger.debug("emit_screenshot: delete carrier failed: %s", e)

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_FT_MORE)]]
    )
    try:
        await bot.send_photo(
            chat_id=user_id,
            photo=io.BytesIO(png_bytes),
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.debug("emit_screenshot send failed: %s", e)


# --- /status + emitter ---


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


async def emit_status(bot: Bot, user_id: int) -> None:
    """Send /status as a fresh message (Menu→Status callback uses a richer
    keyboard variant via callbacks/more_menu.py)."""
    usage = await compute_user_usage(user_id)
    text = format_usage_status(user_id, usage)
    await safe_send(bot, user_id, text)


# --- /list emitter (text body lives in lifecycle.build_live_sessions_text) ---


async def emit_list(bot: Bot, user_id: int) -> None:
    """Render /list as a fresh bot message (used by the Menu→List callback)."""
    body = build_live_sessions_text(user_id)
    if body is None:
        await safe_send(bot, user_id, "No live sessions. Use 🆕 New to create one.")
        return
    keyboard = build_switcher_keyboard(user_id, include_lost=True)
    sent = await safe_send(bot, user_id, body, reply_markup=keyboard)
    if sent and keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)


# --- /usage (interactive Claude TUI) ---


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Fetch Claude Code usage stats from the active session's TUI.

    Note: this hits the *active* session — it's separate from the
    dedicated ccbot-usage window used by Menu→Status (see
    bot/_usage_window.py). Kept for users who want raw modal output.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    wid = active_window(user.id)
    if not wid:
        await safe_reply(update.message, "No active session. Use /new to create one.")
        return

    w = await tmux_manager.find_window_by_id(wid)
    if not w:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    await tmux_manager.send_keys(w.window_id, "/usage")
    await asyncio.sleep(2.0)
    pane_text = await tmux_manager.capture_pane(w.window_id)
    await tmux_manager.send_keys(w.window_id, "Escape", enter=False, literal=False)

    if not pane_text:
        await safe_reply(update.message, "Failed to capture usage info.")
        return

    from ...terminal_parser import parse_usage_output

    usage = parse_usage_output(pane_text)
    if usage and usage.parsed_lines:
        text = "\n".join(usage.parsed_lines)
        await safe_reply(update.message, f"```\n{text}\n```")
    else:
        trimmed = pane_text.strip()
        if len(trimmed) > 3000:
            trimmed = trimmed[:3000] + "\n... (truncated)"
        await safe_reply(update.message, f"```\n{trimmed}\n```")


# --- /health ---


def _format_duration(seconds: float) -> str:
    """Compact wall-clock formatter: 14h32m, 3m17s, etc."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


async def health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/health` — bot uptime, tmux state, queue depth, key counters."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    from ...handlers.message_queue import get_message_queue
    from ...metrics import snapshot

    snap = snapshot()
    counters = snap.get("counters", {})
    obs = snap.get("observations", {})

    windows = await tmux_manager.list_windows()
    live_window_count = len(windows)
    sessions = list(session_manager.sessions.values())
    active = sum(1 for s in sessions if s.state == "active")
    idle = sum(1 for s in sessions if s.state == "idle")
    archived = sum(1 for s in sessions if s.state in ("archived", "completed"))
    lost = sum(1 for s in sessions if s.state == "lost")

    queue = get_message_queue(user.id)
    queue_size = queue.qsize() if queue is not None else 0

    lines = [
        "*Health*",
        f"uptime: {_format_duration(snap.get('uptime_seconds', 0))}",
        "",
        "*tmux*",
        f"windows alive: {live_window_count}",
        "",
        "*sessions*",
        f"active: {active} · idle: {idle} · archived: {archived} · lost: {lost}",
        "",
        "*queue*",
        f"depth (you): {queue_size}",
    ]

    interesting = (
        "tg_messages_in",
        "tg_send_failures",
        "sessions_created",
        "sessions_archived",
        "sessions_completed",
        "session_token_alerts_emitted",
        "quota_alerts_emitted",
    )
    counter_lines = [f"{k}: {counters[k]}" for k in interesting if k in counters]
    if counter_lines:
        lines.append("")
        lines.append("*counters*")
        lines.extend(counter_lines)

    if "tg_to_claude_latency_ms" in obs:
        s = obs["tg_to_claude_latency_ms"]
        lines.append("")
        lines.append("*tg→claude latency (ms)*")
        lines.append(
            f"p50: {s['p50']:.0f} · p95: {s['p95']:.0f} · "
            f"max: {s['max']:.0f} (n={s['count']})"
        )

    await safe_reply(update.message, "\n".join(lines))
