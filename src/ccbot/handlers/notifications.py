"""Live-card notifications for every session (active or background).

A "card" is a single Telegram message that the bot keeps editMessageText-
updating as Claude emits tool calls, thinking blocks, and text chunks. New
TG messages are sent **only** on the user-approved triggers:

  - task completion (final assistant text turn)
  - blocking error (subset of error events)
  - AskUserQuestion / ExitPlanMode (interactive UI is handled elsewhere
    and naturally creates a separate message)
  - session lifecycle (created/restored/archived/done/killed)
  - inbox file received
  - quota warning (G6)
  - long pause: card not updated for >= STALE_CARD_SECONDS — next event
    starts a fresh card
  - card overflow: rendered text would exceed CARD_HARD_LIMIT chars

Implementation. State per (user_id, session.id):
  msg_id, lines (list of CardLine), last_event_ts, finalized

When a tool_result arrives matching a previous tool_use_id, the existing
line is replaced in place rather than appended, keeping the card compact.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from .message_sender import safe_send
from .switcher import build_switcher_keyboard, session_emoji

logger = logging.getLogger(__name__)


# Hard cap for rendered card text — Telegram limit is 4096; leave headroom.
CARD_HARD_LIMIT = 3800
# Number of accumulated lines kept; older lines get summarised away.
CARD_MAX_LINES = 40
# After this much idleness, the next event opens a fresh card.
STALE_CARD_SECONDS = 5 * 60


_COLLAPSED_PREFIX = "… "
_COLLAPSED_SUFFIX = " earlier tool calls collapsed"


@dataclass
class CardLine:
    text: str
    tool_use_id: str | None = None  # so tool_result can edit-replace its tool_use line
    is_tool: bool = False  # true for both tool_use and tool_result lines
    collapsed_count: int = 0  # for the synthetic "… N earlier ..." placeholder


@dataclass
class CardState:
    msg_id: int | None = None
    lines: list[CardLine] = field(default_factory=list)
    last_event_ts: float = 0.0
    status_text: str = ""  # latest tmux status line ("…Esc to interrupt"), shown in header
    last_rendered: str = ""  # last text we sent to TG; lets touch_card_status skip no-op edits


# Per-(user, session.id) card state.
_cards: dict[tuple[int, str], CardState] = {}

# Reverse lookup so reply-quote can route a one-shot user message to the
# session that owns the message being replied to. Capped via FIFO eviction.
_MSG_REGISTRY_LIMIT = 2000
_msg_to_session: dict[tuple[int, int], str] = {}


def _register_msg(user_id: int, message_id: int, session_id: str) -> None:
    """Remember which session a bot message belongs to for reply-quote routing."""
    key = (user_id, message_id)
    # Best-effort eviction: drop ~10% of the oldest entries when the cap
    # is hit. dict preserves insertion order in CPython 3.7+.
    if len(_msg_to_session) >= _MSG_REGISTRY_LIMIT and key not in _msg_to_session:
        drop = max(1, _MSG_REGISTRY_LIMIT // 10)
        for k in list(_msg_to_session.keys())[:drop]:
            _msg_to_session.pop(k, None)
    _msg_to_session[key] = session_id


def lookup_session_for_message(user_id: int, message_id: int) -> str | None:
    """Resolve a Telegram message id back to the Session.id it represents."""
    return _msg_to_session.get((user_id, message_id))


def _trim(s: str, limit: int = 200) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


def _line_for_event(msg: NewMessage) -> CardLine:
    """Build a single one-line summary of one Claude event."""
    text = msg.text or ""
    if msg.content_type == "thinking":
        return CardLine(text=f"∴ {_trim(text, 160)}")
    if msg.content_type == "tool_use":
        tname = msg.tool_name or "tool"
        return CardLine(
            text=f"[{tname}] {_trim(text, 160)}",
            tool_use_id=msg.tool_use_id,
            is_tool=True,
        )
    if msg.content_type == "tool_result":
        tname = msg.tool_name or "result"
        # Show a tiny success marker; the matching tool_use line will be
        # replaced in update_session_card so we don't render two lines per tool.
        return CardLine(
            text=f"[{tname} ✓] {_trim(text, 160)}",
            tool_use_id=msg.tool_use_id,
            is_tool=True,
        )
    # text content (assistant or user echo)
    if msg.role == "user":
        return CardLine(text=f"👤 {_trim(text, 200)}")
    return CardLine(text=_trim(text, 200))


def _collapse_old_tools(state: CardState) -> None:
    """Keep only the last `config.card_visible_tools` tool lines visible.

    Older tool lines are removed and replaced by a single placeholder line
    "… N earlier tool calls collapsed" inserted at the position of the
    earliest dropped tool. Non-tool lines (thinking, text, user echoes)
    are preserved in order.
    """
    cap = max(1, config.card_visible_tools)
    tool_idxs = [i for i, ln in enumerate(state.lines) if ln.is_tool]
    if len(tool_idxs) <= cap:
        # Make sure no stale collapse-placeholder lingers from a previous prune.
        state.lines = [ln for ln in state.lines if ln.collapsed_count == 0]
        return

    keep_set = set(tool_idxs[-cap:])
    new_lines: list[CardLine] = []
    placeholder_inserted = False
    dropped = 0
    earliest_drop_pos: int | None = None
    for i, ln in enumerate(state.lines):
        # Drop existing placeholders — we'll add a fresh one if needed.
        if ln.collapsed_count > 0:
            continue
        if ln.is_tool and i not in keep_set:
            dropped += 1
            if earliest_drop_pos is None:
                earliest_drop_pos = len(new_lines)
            continue
        new_lines.append(ln)

    if dropped > 0 and earliest_drop_pos is not None:
        new_lines.insert(
            earliest_drop_pos,
            CardLine(
                text=f"{_COLLAPSED_PREFIX}{dropped}{_COLLAPSED_SUFFIX}",
                collapsed_count=dropped,
            ),
        )
        placeholder_inserted = True
    elif dropped > 0 and not placeholder_inserted:
        new_lines.append(
            CardLine(
                text=f"{_COLLAPSED_PREFIX}{dropped}{_COLLAPSED_SUFFIX}",
                collapsed_count=dropped,
            )
        )

    state.lines = new_lines


def _render_card(sess: Session, state: CardState, *, footer: str = "") -> str:
    emoji = session_emoji(sess)
    state_label = sess.state
    if state.status_text:
        # Promote the running tmux status into the header so we don't need a
        # separate ephemeral "Esc to interrupt" message.
        state_label = f"{state_label} · {_trim(state.status_text, 60)}"
    header = f"{emoji} *{sess.name or sess.id}* · {state_label}"
    if sess.goal:
        header += f"\ngoal: {sess.goal}"
    body = "\n".join(line.text for line in state.lines)
    parts = [header, "─────"]
    if body:
        parts.append(body)
    if footer:
        parts.append("─────")
        parts.append(footer)
    return "\n".join(parts)


def _ensure_room(sess: Session, state: CardState) -> bool:
    """Trim oldest lines while the rendered card exceeds CARD_HARD_LIMIT.

    Returns True if a fresh card should be opened (we trimmed everything
    and still don't fit, or we've crossed CARD_MAX_LINES).
    """
    while len(state.lines) > CARD_MAX_LINES:
        state.lines.pop(0)
    while state.lines and len(_render_card(sess, state)) > CARD_HARD_LIMIT:
        state.lines.pop(0)
    return len(state.lines) <= 1 and len(_render_card(sess, state)) > CARD_HARD_LIMIT


def _is_stale(state: CardState) -> bool:
    if state.msg_id is None or state.last_event_ts <= 0:
        return False
    return (time.time() - state.last_event_ts) >= STALE_CARD_SECONDS


def get_card_state(user_id: int, sess: Session) -> CardState:
    return _cards.setdefault((user_id, sess.id), CardState())


def reset_card(user_id: int, session_id: str) -> None:
    """Drop the cached card so the next event creates a fresh message."""
    _cards.pop((user_id, session_id), None)


async def _send_card(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
) -> None:
    """Send a brand-new card message and remember it as the live card."""
    keyboard = build_switcher_keyboard(user_id)
    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=text,
            reply_markup=keyboard,
            disable_notification=True,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.debug("card send failed: %s", e)
        return
    if not sent:
        return
    # Strip the previous switcher (if any), then remember this one as the
    # carrier of the live switcher.
    prev = session_manager.get_last_switcher_msg(user_id)
    if prev and prev != sent.message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id, message_id=prev, reply_markup=None
            )
        except Exception:
            pass
    if keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)
    state.msg_id = sent.message_id
    state.last_rendered = text
    _register_msg(user_id, sent.message_id, sess.id)


async def _edit_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
) -> bool:
    """Edit the live card. Returns False if the edit failed permanently."""
    if state.msg_id is None:
        return False
    try:
        await bot.edit_message_text(
            chat_id=user_id, message_id=state.msg_id, text=text
        )
        return True
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return True
        logger.debug("card edit failed (BadRequest): %s", e)
    except RetryAfter:
        raise
    except Exception as e:
        logger.debug("card edit failed (other): %s", e)
    return False


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Append `msg` to the session's live card, or open a new one if needed.

    Triggers a fresh card on long pause and on hard-limit overflow.
    """
    state = get_card_state(user_id, sess)

    # Trigger: long pause → fresh card.
    if _is_stale(state):
        state.msg_id = None
        state.lines = []

    new_line = _line_for_event(msg)

    # tool_result: replace the matching tool_use line in place.
    replaced = False
    if msg.content_type == "tool_result" and msg.tool_use_id:
        for i, ln in enumerate(state.lines):
            if ln.tool_use_id == msg.tool_use_id:
                state.lines[i] = new_line
                replaced = True
                break
    if not replaced:
        state.lines.append(new_line)

    state.last_event_ts = time.time()

    # Cap visible tool history per CARD_VISIBLE_TOOLS.
    _collapse_old_tools(state)

    # Trigger: card overflow → continuation card.
    overflow = _ensure_room(sess, state)
    if overflow:
        # Couldn't fit even one line — start a fresh card holding only the new line.
        state.msg_id = None
        state.lines = [new_line]

    text = _render_card(sess, state)

    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text)
        return
    edited = await _edit_card(bot, user_id, state, text=text)
    if edited:
        state.last_rendered = text
    else:
        # Lost the message — start a new card.
        state.msg_id = None
        await _send_card(bot, user_id, sess, state, text=text)


async def finalize_task(
    bot: Bot, user_id: int, sess: Session, final_text: str
) -> None:
    """Mark the current task as complete: flush card, send completion push,
    drop the live-card pointer so the next event opens a fresh card.
    """
    state = get_card_state(user_id, sess)
    final_line = CardLine(text=f"✓ {_trim(final_text, 600)}")
    state.lines.append(final_line)
    state.last_event_ts = time.time()
    _ensure_room(sess, state)
    text = _render_card(sess, state, footer="(task complete)")
    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text)
    else:
        await _edit_card(bot, user_id, state, text=text)
    # Reset for next task — a new card will spawn on the next event.
    reset_card(user_id, sess.id)
    # Push completion summary as a separate message.
    summary = _trim(final_text, 200) or "task complete"
    await push_event(bot, user_id, sess, text=f"✓ {summary}")


async def push_event(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    text: str,
    is_error: bool = False,
) -> None:
    """C5 push notification — separate `send_message` for one of the
    user-approved triggers (completion / blocker error / interactive UI /
    lifecycle / inbox / quota).
    """
    emoji = "🟥" if is_error else session_emoji(sess)
    body = f"{emoji} \\[{sess.name or sess.id}\\] {text}"
    if len(body) > 3500:
        body = body[:3497] + "…"
    try:
        sent = await safe_send(bot, user_id, body)
    except Exception as e:
        logger.debug("push_event failed: %s", e)
        return
    # Migrate the switcher onto the latest pushed message.
    if sent is not None:
        _register_msg(user_id, sent.message_id, sess.id)
        keyboard: InlineKeyboardMarkup | None = build_switcher_keyboard(user_id)
        if keyboard is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=sent.message_id, reply_markup=keyboard
                )
                prev = session_manager.get_last_switcher_msg(user_id)
                if prev and prev != sent.message_id:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=user_id, message_id=prev, reply_markup=None
                        )
                    except Exception:
                        pass
                session_manager.set_last_switcher_msg(user_id, sent.message_id)
            except Exception as e:
                logger.debug("push_event switcher migrate failed: %s", e)


def is_active_for_user(user_id: int, sess: Session) -> bool:
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


async def touch_card_status(
    bot: Bot, user_id: int, window_id: str, status_text: str
) -> None:
    """Update the card header's status badge for the session that owns
    `window_id`. No-op when there is no live card or the badge text is
    unchanged. Does not create a card if none exists.
    """
    sess = session_manager.find_session_by_window(window_id)
    if sess is None:
        return
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        # No live card to update — don't create one for a status tick.
        return
    if state.status_text == status_text:
        return
    state.status_text = status_text
    rendered = _render_card(sess, state)
    if rendered == state.last_rendered:
        return
    if await _edit_card(bot, user_id, state, text=rendered):
        state.last_rendered = rendered
