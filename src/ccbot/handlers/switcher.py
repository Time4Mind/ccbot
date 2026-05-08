"""Inline session switcher (A8) — keyboard, attach/strip, context preview.

Builds the inline-keyboard row of session buttons that appears under the most
recent bot message. When a new bot message arrives, the previous switcher's
reply markup is stripped so only the latest message carries the switcher.

Public API:
  build_switcher_keyboard(user_id) -> InlineKeyboardMarkup | None
  attach_switcher(bot, user_id, message_id) -> None
  strip_active_switcher(bot, user_id) -> None
  build_session_preview(sess) -> str

State of "where the live switcher currently lives" is held in
session_manager.last_switcher_msg_id (persisted in state.json).
"""

from __future__ import annotations

import logging

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest

from ..config import config
from ..session import Session, session_manager
from .callback_data import CB_SW_NEW, CB_SW_NOOP, CB_SW_USE

logger = logging.getLogger(__name__)


# Twelve color emoji used as visual session markers; assigned by name hash.
_SESSION_EMOJI: tuple[str, ...] = (
    "🟦",
    "🟩",
    "🟨",
    "🟧",
    "🟥",
    "🟪",
    "🟫",
    "⬛",
    "🔵",
    "🟢",
    "🟣",
    "🟠",
)


def session_emoji(sess: Session) -> str:
    """Stable color marker for a session, hashed from its id."""
    h = sum(ord(c) for c in sess.id) if sess.id else 0
    return _SESSION_EMOJI[h % len(_SESSION_EMOJI)]


def _label(sess: Session, *, is_active: bool) -> str:
    """Render a button label for a session in the switcher."""
    name = sess.name or sess.id
    # Trim long names so several fit on one row
    if len(name) > 14:
        name = name[:13] + "…"
    emoji = session_emoji(sess)
    if is_active:
        return f"✓ {emoji} {name}"
    return f"{emoji} {name}"


def build_switcher_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    """Build the inline switcher keyboard for a user.

    Returns None if the user has no active or idle sessions and we are not in
    a state where a "+ new" button alone is appropriate. We always include the
    "+ new" button when at least one session exists; when there are zero
    sessions we return None (the caller decides whether to surface a "+ new"
    elsewhere, e.g. via /new).
    """
    active = session_manager.list_user_sessions(user_id, states=("active", "idle"))
    if not active:
        return None

    active_sess = session_manager.get_active_session(user_id)
    active_id = active_sess.id if active_sess else ""

    # 3 buttons per row; sessions then a final "+ new"
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for sess in active:
        is_active = sess.id == active_id
        cb = CB_SW_NOOP if is_active else f"{CB_SW_USE}{sess.id}"
        row.append(
            InlineKeyboardButton(
                _label(sess, is_active=is_active),
                callback_data=cb[:64],
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("+ new", callback_data=CB_SW_NEW)])
    return InlineKeyboardMarkup(rows)


async def strip_active_switcher(bot: Bot, user_id: int) -> None:
    """Strip the inline keyboard from the user's previously-attached switcher
    message, if any. Cheap no-op if nothing is tracked.
    """
    msg_id = session_manager.get_last_switcher_msg(user_id)
    if msg_id is None:
        return
    try:
        await bot.edit_message_reply_markup(
            chat_id=user_id, message_id=msg_id, reply_markup=None
        )
    except BadRequest as e:
        # Most common: message too old, message is not modified.
        logger.debug("strip_active_switcher: %s", e)
    except Exception as e:
        logger.debug("strip_active_switcher unexpected: %s", e)
    finally:
        session_manager.clear_last_switcher_msg(user_id)


async def attach_switcher(bot: Bot, user_id: int, message_id: int) -> None:
    """Attach the current switcher to `message_id` and strip the previous one.

    No-op if the user has no sessions (returns silently).
    """
    keyboard = build_switcher_keyboard(user_id)
    if keyboard is None:
        return

    prev_id = session_manager.get_last_switcher_msg(user_id)
    if prev_id and prev_id != message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id, message_id=prev_id, reply_markup=None
            )
        except Exception as e:
            logger.debug("attach_switcher: prev strip failed: %s", e)

    try:
        await bot.edit_message_reply_markup(
            chat_id=user_id, message_id=message_id, reply_markup=keyboard
        )
        session_manager.set_last_switcher_msg(user_id, message_id)
    except BadRequest as e:
        # Telegram returns "Message is not modified" if keyboard didn't change.
        # That's still success from our perspective — the keyboard is on it.
        if "Message is not modified" in str(e):
            session_manager.set_last_switcher_msg(user_id, message_id)
            return
        logger.debug("attach_switcher: edit failed: %s", e)
    except Exception as e:
        logger.debug("attach_switcher: unexpected: %s", e)


def build_session_preview(
    sess: Session,
    *,
    last_user: str = "",
    last_assistant: str = "",
    last_tools: list[str] | None = None,
) -> str:
    """Render a short context preview for a session, used on switch.

    The bot edits the message in place to show this preview. Length-bounded by
    PREVIEW_USER_LINES / PREVIEW_ASSISTANT_LINES / PREVIEW_TOOLS env vars.
    """
    parts: list[str] = []
    emoji = session_emoji(sess)
    state_label = sess.state if sess.state else "?"
    usage = (
        f"{sess.token_usage_total // 1000}k tok" if sess.token_usage_total else "0 tok"
    )
    header = f"{emoji} {sess.name or sess.id} · {state_label} · {usage}"
    if sess.goal:
        header += f"\ngoal: {sess.goal}"
    parts.append(header)
    parts.append("─" * 16)

    if last_user:
        parts.append("You: " + _trim_lines(last_user, config.preview_user_lines))
    if last_assistant:
        parts.append(
            "Claude: " + _trim_lines(last_assistant, config.preview_assistant_lines)
        )
    if last_tools:
        for tool in last_tools[: config.preview_tools]:
            parts.append(tool)

    return "\n".join(parts)


def _trim_lines(text: str, max_lines: int) -> str:
    if max_lines <= 0:
        return text
    lines = text.splitlines()
    if len(lines) <= max_lines:
        return text
    head = "\n".join(lines[:max_lines])
    return head + "\n…"
