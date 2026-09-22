"""Read-only info commands: /usage, /health, /help.

These also expose ``render_help`` used by the inline Help callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ...handlers.message_sender import safe_reply
from ...i18n import t
from ...session import session_manager
from ...terminal_runtime import (
    PaneCaptureError,
    capture_session_pane,
    send_session_key,
)
from ...tmux_manager import tmux_manager
from .._common import active_window, is_user_allowed

logger = logging.getLogger(__name__)


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
    active = session_manager.get_active_session(user.id)
    if active is not None and active.backend == "codex":
        await safe_reply(
            update.message,
            "Codex usage quota is not exposed through the Claude /usage parser.",
        )
        return

    wid = active_window(user.id)
    if not wid:
        await safe_reply(update.message, "No active session. Use /new to create one.")
        return

    if active is None:
        await safe_reply(update.message, f"Window '{wid}' no longer exists.")
        return

    success, message = await session_manager.send_to_window(wid, "/usage")
    if not success:
        await safe_reply(update.message, f"Failed to request usage info: {message}")
        return
    await asyncio.sleep(2.0)
    try:
        pane_text = await capture_session_pane(active)
    except PaneCaptureError:
        pane_text = ""
    await send_session_key(active, "Escape")

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
