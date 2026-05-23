"""Inline session switcher (A8) — keyboard, strip, context preview.

Builds the inline-keyboard row of session buttons that appears under the most
recent bot message. When a new bot message arrives, the previous switcher's
reply markup is stripped so only the latest message carries the switcher.

Public API:
  build_switcher_keyboard(user_id) -> InlineKeyboardMarkup | None
  build_session_preview(sess) -> str

State of "where the live switcher currently lives" is held in
session_manager.last_switcher_msg_id (persisted in state.json).
"""

from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import Session, session_manager
from .callback_data import CB_ARC_RESTORE, CB_SW_NEW, CB_SW_USE

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


def build_switcher_keyboard(
    user_id: int, *, include_lost: bool = False, include_new: bool = True
) -> InlineKeyboardMarkup | None:
    """Build the inline switcher keyboard for a user.

    With `include_lost=True`, lost sessions also appear as buttons whose
    callback restores the session (CB_ARC_RESTORE) per spec §9 (F2).
    Default behavior (active+idle only) is unchanged for content-message
    attachments.

    `include_new` controls whether the trailing "+ new" row is appended.
    Callers that want to place "+ new" next to another bottom-row button
    (≡ Menu / Back) pass `include_new=False` and add the pair themselves.

    Returns None if the user has no live (or lost, when included) sessions.
    """
    states = ("active", "idle", "lost") if include_lost else ("active", "idle")
    sessions = session_manager.list_user_sessions(user_id, states=states)
    if not sessions:
        return None

    active_sess = session_manager.get_active_session(user_id)
    active_id = active_sess.id if active_sess else ""

    # 3 buttons per row; sessions then a final "+ new"
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for sess in sessions:
        is_active = sess.id == active_id
        if sess.state == "lost":
            # Lost sessions can't be switched to — tap = restore.
            name = sess.name or sess.id
            if len(name) > 12:
                name = name[:11] + "…"
            label = f"⤴ {name}"
            cb = f"{CB_ARC_RESTORE}{sess.id}"
        else:
            # All session buttons (including the already-active one) go
            # through CB_SW_USE so a tap always lands on the session's
            # history. The switcher.CB_SW_USE handler's
            # ``transfer_card_to_carrier`` short-circuits the same-session
            # case to a no-op, then ``send_history`` paints. Without this,
            # there was no way to view the active session's transcript
            # from the switcher — the ``✓`` button just toasted
            # "already active" and the user couldn't see the history.
            cb = f"{CB_SW_USE}{sess.id}"
            label = _label(sess, is_active=is_active)
        row.append(
            InlineKeyboardButton(
                label,
                callback_data=cb[:64],
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if include_new:
        rows.append([InlineKeyboardButton("+ new", callback_data=CB_SW_NEW)])
    return InlineKeyboardMarkup(rows)


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
    header = f"{emoji} {sess.name or sess.id} · {state_label}"
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
