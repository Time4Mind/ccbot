"""C5 + C7 notifications — per-session live card and push messages.

C5 (push). Sent as a fresh send_message on key events only:
  - assistant completion (final text turn)
  - error / failure
  - AskUserQuestion / ExitPlanMode
Format: `<emoji> [<session-name>] <text>`.

C7 (live card). One message per (user, session) that the bot keeps
editMessageText-updating with the latest tool/event. Cheap because TG edits
do not raise a notification on the user's device.

For the **active** session we keep the existing multi-message stream
(handle_new_message goes through enqueue_content_message + switcher).
For **background** sessions we use the live-card mechanism here, gated by
config.bg_notify_mode:
  - "separate" (default): each background session has its own live card.
  - "footer":  append a one-line per-session footer to the active session's
    most recent bot message instead of a card. Implemented as a fall-through
    to "separate" in v0.1; will be enabled once we have a stable hook to
    edit the active session's card without races (Phase 6 follow-up).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from .message_sender import safe_send
from .switcher import session_emoji

logger = logging.getLogger(__name__)


# Per-(user, session) live card state.
# Key: (user_id, session.id). Value: (message_id, last_rendered_text).
_session_cards: dict[tuple[int, str], tuple[int, str]] = {}


@dataclass
class _CardEvent:
    """Compact single-line summary of the latest event for a session."""

    line: str
    final: str = ""


def _summarize(msg: NewMessage) -> _CardEvent:
    """Reduce a NewMessage into a one-line summary for the live card."""
    head = msg.text or ""
    head = head.replace("\n", " ").strip()
    if len(head) > 200:
        head = head[:197] + "…"
    if msg.content_type == "tool_use":
        tname = msg.tool_name or "tool"
        return _CardEvent(line=f"[{tname}] {head}")
    if msg.content_type == "tool_result":
        tname = msg.tool_name or "result"
        return _CardEvent(line=f"[{tname} ✓] {head}")
    if msg.content_type == "thinking":
        return _CardEvent(line=f"∴ {head}")
    final = (msg.text or "").strip() if msg.is_complete and msg.role == "assistant" else ""
    return _CardEvent(line=head, final=final)


def render_card(sess: Session, event: _CardEvent | None) -> str:
    """Render a session's live card text."""
    emoji = session_emoji(sess)
    state = sess.state
    parts = [f"{emoji} *{sess.name or sess.id}* · {state}"]
    if sess.goal:
        parts.append(f"goal: {sess.goal}")
    if event is not None:
        parts.append("───")
        if event.line:
            parts.append(f"last: {event.line}")
        if event.final:
            f = event.final
            if len(f) > 1500:
                f = f[:1497] + "…"
            parts.append("─ result ─")
            parts.append(f)
    return "\n".join(parts)


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Edit-or-send the per-session live card with the latest event.

    Cheap on rate limits: editMessageText does not push a notification.
    """
    event = _summarize(msg)
    text = render_card(sess, event)
    key = (user_id, sess.id)

    existing = _session_cards.get(key)
    if existing is not None:
        msg_id, last_text = existing
        if last_text == text:
            return
        try:
            await bot.edit_message_text(chat_id=user_id, message_id=msg_id, text=text)
            _session_cards[key] = (msg_id, text)
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                _session_cards[key] = (msg_id, text)
                return
            logger.debug("card edit failed: %s — sending new", e)
        except RetryAfter:
            raise
        except Exception as e:
            logger.debug("card edit unexpected: %s — sending new", e)

    # Send a fresh card.
    try:
        sent = await bot.send_message(
            chat_id=user_id, text=text, disable_notification=True
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.debug("card send failed: %s", e)
        return
    if sent:
        _session_cards[key] = (sent.message_id, text)


def reset_card(user_id: int, session_id: str) -> None:
    """Drop the cached card so the next event creates a fresh message."""
    _session_cards.pop((user_id, session_id), None)


async def push_event(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    text: str,
    is_error: bool = False,
) -> None:
    """C5 push — `<emoji> [<name>] <text>` as a separate send_message.

    Reserved for completion / error / AskUserQuestion only. Cards do not
    trigger pushes.
    """
    emoji = session_emoji(sess)
    if is_error:
        emoji = "🟥"
    body = f"{emoji} \\[{sess.name or sess.id}\\] {text}"
    if len(body) > 3500:
        body = body[:3497] + "…"
    try:
        await safe_send(bot, user_id, body)
    except Exception as e:
        logger.debug("push_event failed: %s", e)


def is_active_for_user(user_id: int, sess: Session) -> bool:
    """True iff `sess` is the user's currently-active session."""
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


def bg_mode() -> str:
    """Return the configured BG_NOTIFY_MODE; falls back to 'separate'."""
    mode = (config.bg_notify_mode or "separate").lower()
    if mode not in ("separate", "footer"):
        return "separate"
    return mode
