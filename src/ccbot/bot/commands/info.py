"""Read-only info commands: /status, /history, /screenshot, /usage, /health, /help.

These also expose ``emit_*`` / ``render_help`` helpers used by the
inline Menu and Help callbacks when the user opens the same view from
a button instead of a slash command. ``build_screenshot_keyboard``
lives here because both the /screenshot command and the CB_KEYS_* /
CB_SCREENSHOT_REFRESH callback paths need it.
"""

from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_SHOT_BACK,
    CB_SHOT_SW,
)
from ...handlers.history import send_history
from ...handlers.message_sender import safe_reply, safe_send
from ...handlers.switcher import session_emoji
from ...i18n import t
from ...screenshot import text_to_image
from ...session import session_manager
from ...tmux_manager import tmux_manager
from ...usage import format_usage_breakdown_compact
from .._common import active_window, is_user_allowed

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


def _shot_session_label(sess: Any, *, is_active: bool) -> str:
    name = sess.name or sess.id
    if len(name) > 14:
        name = name[:13] + "…"
    emoji = session_emoji(sess)
    if is_active:
        return f"✓ {emoji} {name}"
    return f"{emoji} {name}"


def build_screenshot_compact_keyboard(
    user_id: int, origin: str, *, mode: str = "s"
) -> InlineKeyboardMarkup:
    """Inline keyboard for the compact screenshot view.

    Two modes, toggled by the bottom-row button:

    * ``mode="s"`` (switcher, default) — session-switcher rows + a
      ``[⌨ Keys] [Back]`` pair. Tapping a session redraws the photo
      with that session's pane.
    * ``mode="k"`` (keyboard) — arrow / Enter / Esc / Tab / Space / ^C
      key grid that send-keys into the **active** session's window
      (same semantics as the ``/screenshot`` control keyboard), plus a
      ``[← Switcher] [Back]`` pair. Lets the user navigate an
      AskUserQuestion-style prompt without typing.

    ``origin`` is ``"m"`` (main / live card) or ``"l"`` (Menu → List
    view); the Back button routes back to that surface in both modes.
    """
    # Manual ⌨ kb-mode toggle removed (Task #41) — kb-mode comes up
    # automatically on the live card when claude shows a prompt, no
    # need for a button in the shot photo. The photo keyboard is just
    # session-switcher + Back now.
    del mode  # legacy param, ignored
    active_sess = session_manager.get_active_session(user_id)
    sessions = session_manager.list_user_sessions(user_id, states=("active", "idle"))
    active_id = active_sess.id if active_sess is not None else ""
    rows = []
    row: list[InlineKeyboardButton] = []
    for sess in sessions:
        label = _shot_session_label(sess, is_active=sess.id == active_id)
        cb = f"{CB_SHOT_SW}{sess.id}"
        row.append(InlineKeyboardButton(label, callback_data=cb[:64]))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=f"{CB_SHOT_BACK}{origin}",
            ),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def emit_screenshot_compact(
    query: Any, bot: Bot, user_id: int, *, origin: str = "m"
) -> None:
    """Send a compressed photo of the active session's pane as a NEW
    chat message, and CLOSE the live card so claude events spawn a
    fresh message instead of editing the now-orphaned carrier.

    Task #51: switched from ``pause_card_view`` (kept msg_id, edited
    in place on resume) to ``close_card_view`` (drops msg_id, strips
    reply markup on the old carrier). When the user taps Back, a NEW
    card lands below the screenshot — replacement of one message by
    another — instead of the resume-edit hitting a stale far-up
    message that Telegram might refuse to edit anyway.
    """
    from ...handlers.notifications import close_card_view
    from ...session import session_manager

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

    # Close the active card so claude events don't try to edit it
    # underneath while the user is looking at the screenshot. The next
    # event after Back creates a fresh card (replacement of one
    # message by another). See ``close_card_view``.
    active = session_manager.get_active_session(user_id)
    if active is not None:
        await close_card_view(bot, user_id, active.id)

    keyboard = build_screenshot_compact_keyboard(user_id, origin)
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
    """`/status` — live usage from Claude's own ``/usage`` modal.

    Uses the same fetch path as Menu→Status: a dedicated ccbot-usage
    tmux window pops ``/usage``, captures the rendered modal, and we
    parse it. The locally-aggregated JSONL counter was misleading
    (didn't reflect Claude's authoritative window/weekly counters), so
    we now report only what Claude itself reports.
    """
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    from ...handlers.message_sender import safe_edit

    placeholder = await safe_reply(update.message, t(user.id, "usage.fetching"))
    text = await _format_live_usage(user.id)
    await safe_edit(placeholder, text)


async def emit_status(bot: Bot, user_id: int) -> None:
    """Send /status as a fresh message (Menu→Status callback uses a richer
    keyboard variant via callbacks/more_menu.py)."""
    placeholder = await safe_send(bot, user_id, t(user_id, "usage.fetching"))
    text = await _format_live_usage(user_id)
    if placeholder is not None:
        from ...handlers.message_sender import safe_edit

        await safe_edit(placeholder, text)
    else:
        await safe_send(bot, user_id, text)


async def _format_live_usage(user_id: int) -> str:
    """Run Claude's /usage modal in the dedicated window and render its
    parsed breakdown. Falls back to a short ``unavailable`` notice when
    parsing fails — same surface as Menu→Status."""
    from .._usage_window import fetch_claude_usage

    info = await fetch_claude_usage()
    block = format_usage_breakdown_compact(user_id, info)
    return block or t(user_id, "usage.unavailable")


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

    lines = [
        "*Health*",
        f"uptime: {_format_duration(snap.get('uptime_seconds', 0))}",
        "",
        "*tmux*",
        f"windows alive: {live_window_count}",
        "",
        "*sessions*",
        f"active: {active} · idle: {idle} · archived: {archived} · lost: {lost}",
    ]

    interesting = (
        "tg_messages_in",
        "tg_send_failures",
        "sessions_created",
        "sessions_archived",
        "sessions_completed",
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


# --- /help — inline mini-doc with section buttons ---


HELP_SECTIONS: tuple[str, ...] = (
    "overview",
    "sessions",
    "menu",
    "commands",
    "voice",
    "alerts",
    "terminal",
    "tips",
)


def render_help(
    user_id: int, section: str = "home"
) -> tuple[str, InlineKeyboardMarkup]:
    """Build (text, keyboard) for either the top-level help screen or one section.

    The home screen lists the section buttons. A section screen renders the
    body for that section plus a back row that returns to home / Menu.
    """
    from ...handlers.callback_data import CB_HLP_HOME, CB_HLP_SEC, CB_MM_BACK

    if section == "home":
        text = t(user_id, "help.home.body")
    elif section in HELP_SECTIONS:
        text = t(user_id, f"help.body.{section}")
    else:
        text = t(user_id, "help.home.body")
        section = "home"

    section_buttons: list[InlineKeyboardButton] = []
    for s in HELP_SECTIONS:
        label = t(user_id, f"help.btn.{s}")
        if s == section:
            label = f"• {label}"
        section_buttons.append(
            InlineKeyboardButton(label, callback_data=f"{CB_HLP_SEC}{s}")
        )

    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(section_buttons), 2):
        rows.append(section_buttons[i : i + 2])

    if section == "home":
        rows.append(
            [InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_MM_BACK)]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_HLP_HOME),
                InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_MM_BACK),
            ]
        )
    return text, InlineKeyboardMarkup(rows)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """`/help` — open the inline mini-doc. Other sections are reachable via taps."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return
    text, keyboard = render_help(user.id, "home")
    await safe_reply(update.message, text, reply_markup=keyboard)
