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

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from ..telegram_sender import split_message
from . import bg_status
from .message_sender import safe_send
from .menu import build_footer_keyboard
from .switcher import session_emoji
from .tg_format import Attachment, split_overflow

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
    status_text: str = (
        ""  # latest tmux status line ("…Esc to interrupt"), shown in header
    )
    last_rendered: str = (
        ""  # last text we sent to TG; lets touch_card_status skip no-op edits
    )
    last_edit_ts: float = 0.0  # monotonic seconds; gate for CARD_EDIT_LAG coalescing
    pending_edit: asyncio.Task[None] | None = None  # one deferred edit task at most
    is_continuation: bool = False  # True after a stale-pause or overflow split
    # User opened ≡ Menu / a sub-screen on the card's message. While set,
    # session updates accumulate into ``lines`` but are NOT rendered to
    # Telegram — otherwise the next event would overwrite whatever menu
    # screen the user is looking at. Cleared by ``resume_card_view``
    # (called from text_handler when the user types) or implicitly
    # when the card is reset.
    in_menu_view: bool = False
    # Set by ``finalize_task`` while ``in_menu_view`` is True so the
    # delayed render on resume picks up the ``(task complete)`` footer
    # and the cleanup that would normally happen at finalize time runs.
    pending_complete_footer: str | None = None


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


_EXPQUOTE_BLOCK_RE = re.compile(
    r"\x02EXPQUOTE_START\x02.*?\x02EXPQUOTE_END\x02",
    re.DOTALL,
)


def _strip_for_card(text: str) -> str:
    """Drop expandable-quote sentinels + their content from a card line.

    The quote sentinels ``\x02EXPQUOTE_START\x02 … \x02EXPQUOTE_END\x02``
    are meant for the history view's MarkdownV2 renderer. On the live
    card each event is a one-liner, and a 160-char trim happily cuts
    through the middle of a sentinel — leaving raw ``EXPQUOTE_S…`` in
    the chat. Strip the whole quote block here; the user gets the
    one-line summary (``Output 64 lines``) without the noise.

    Also collapses ``$HOME`` to ``~`` so a Bash command line on a Mac
    doesn't waste 30+ chars on ``/Users/<user>/...``.
    """
    import os

    out = _EXPQUOTE_BLOCK_RE.sub("", text)
    home = os.path.expanduser("~")
    if home and home != "/":
        out = out.replace(home, "~")
    return out


async def _seed_prior_context_lines(sess: Session) -> list[CardLine]:
    """Build up to ``config.card_prior_context`` CardLines from the
    transcript entries that came *before* the user's most recent message.

    Used to seed a freshly-opened live card so the user doesn't lose all
    on-screen context every time the previous task finalises and the
    next turn starts blank.

    Returns an empty list when:
      - ``CARD_PRIOR_CONTEXT == 0`` (feature disabled)
      - the session has no window or no transcript yet
      - no user message has been recorded yet (first turn)
      - reading the transcript fails for any reason
    """
    n = config.card_prior_context
    if n <= 0 or not sess.window_id:
        return []
    try:
        messages, _total = await session_manager.get_recent_messages(sess.window_id)
    except Exception as e:
        logger.debug("seed_prior_context: get_recent_messages failed: %s", e)
        return []
    if not messages:
        return []
    # Walk backwards to find the last role=user entry — everything before
    # that is "prior context" relative to the current turn. If no user
    # entry exists yet (e.g. assistant first turn), there's nothing to
    # contextualise; bail.
    last_user_idx: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user_idx = i
            break
    if last_user_idx is None or last_user_idx == 0:
        return []
    head = messages[max(0, last_user_idx - n) : last_user_idx]
    lines: list[CardLine] = []
    for entry in head:
        role = entry.get("role", "")
        ctype = entry.get("content_type", "text")
        text = _strip_for_card(entry.get("text", "") or "")
        if not text.strip():
            continue
        if role == "user":
            lines.append(CardLine(text=f"👤 {_trim(text, 200)}"))
        elif ctype == "thinking":
            lines.append(CardLine(text=f"∴ {_trim(text, 160)}"))
        elif ctype == "tool_use":
            lines.append(CardLine(text=f"▷ {_trim(text, 160)}", is_tool=True))
        elif ctype == "tool_result":
            lines.append(CardLine(text=f"✓ {_trim(text, 160)}", is_tool=True))
        else:
            lines.append(CardLine(text=_trim(text, 200)))
    return lines


def _line_for_event(msg: NewMessage) -> CardLine:
    """Build a single one-line summary of one Claude event.

    Glyph prefix conveys the event type without the verbose
    ``[<tool> ✓]`` framing the body already implies via the bold
    ``**Tool**(...)`` summary:

      ∴  thinking
      ▷  tool_use (in flight)
      ✓  tool_result (completed; will replace the ▷ line in place)
      👤 user-echoed text
      (no prefix) assistant text
    """
    text = _strip_for_card(msg.text or "")
    if msg.content_type == "thinking":
        return CardLine(text=f"∴ {_trim(text, 160)}")
    if msg.content_type == "tool_use":
        return CardLine(
            text=f"▷ {_trim(text, 160)}",
            tool_use_id=msg.tool_use_id,
            is_tool=True,
        )
    if msg.content_type == "tool_result":
        return CardLine(
            text=f"✓ {_trim(text, 160)}",
            tool_use_id=msg.tool_use_id,
            is_tool=True,
        )
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


def _render_card(
    sess: Session,
    state: CardState,
    *,
    footer: str = "",
    user_id: int | None = None,
) -> str:
    emoji = session_emoji(sess)
    state_label = sess.state
    if state.status_text:
        # Promote the running tmux status into the header so we don't need a
        # separate ephemeral "Esc to interrupt" message.
        state_label = f"{state_label} · {_trim(state.status_text, 60)}"
    cont_marker = " · …continued" if state.is_continuation else ""
    # Active-session quota glyph in the header — ⚠️🟢/🟡/🔴 when the
    # session has crossed a usage threshold. Same data source as the
    # bg-panel rows for non-active sessions.
    quota_glyph = ""
    if user_id is not None:
        quota_glyph = bg_status.quota_glyph_for(user_id, sess.id)
    # Last-event timestamp — HH:MM in the bot host's local time, included
    # so the user can tell at a glance whether the card is fresh or has
    # been static for a while. Updates naturally when an event bumps
    # ``last_event_ts``; spinner-only ticks via ``touch_card_status``
    # don't move it, so a 10-minute silent session shows a stale stamp
    # and the user knows nothing has happened.
    ts_suffix = ""
    if state.last_event_ts > 0:
        import datetime as _dt

        ts_suffix = " · " + _dt.datetime.fromtimestamp(state.last_event_ts).strftime(
            "%H:%M"
        )
    name_part = sess.name or sess.id
    if quota_glyph:
        header = (
            f"{emoji} *{name_part}* {quota_glyph} · "
            f"{state_label}{cont_marker}{ts_suffix}"
        )
    else:
        header = f"{emoji} *{name_part}* · {state_label}{cont_marker}{ts_suffix}"
    if sess.goal:
        header += f"\ngoal: {sess.goal}"
    body = "\n".join(line.text for line in state.lines)
    parts = [header, "─────"]
    if body:
        parts.append(body)
    if footer:
        parts.append("─────")
        parts.append(footer)
    # Background-session panel — always at the very bottom of the message
    # so a finished session in the background isn't lost above a long
    # tool-call log. Empty string when the user has no bg sessions to
    # show.
    if user_id is not None:
        panel = bg_status.render_panel(user_id, active_session_id=sess.id)
        if panel:
            parts.append(panel)
    return "\n".join(parts)


def _ensure_room(
    sess: Session, state: CardState, *, user_id: int | None = None
) -> bool:
    """Trim oldest lines while the rendered card exceeds CARD_HARD_LIMIT.

    Returns True if a fresh card should be opened (we trimmed everything
    and still don't fit, or we've crossed CARD_MAX_LINES).
    """
    while len(state.lines) > CARD_MAX_LINES:
        state.lines.pop(0)
    while (
        state.lines
        and len(_render_card(sess, state, user_id=user_id)) > CARD_HARD_LIMIT
    ):
        state.lines.pop(0)
    return (
        len(state.lines) <= 1
        and len(_render_card(sess, state, user_id=user_id)) > CARD_HARD_LIMIT
    )


def _is_stale(state: CardState) -> bool:
    if state.msg_id is None or state.last_event_ts <= 0:
        return False
    return (time.time() - state.last_event_ts) >= STALE_CARD_SECONDS


def get_card_state(user_id: int, sess: Session) -> CardState:
    return _cards.setdefault((user_id, sess.id), CardState())


def reset_card(user_id: int, session_id: str) -> None:
    """Drop the cached card so the next event creates a fresh message."""
    _cards.pop((user_id, session_id), None)


def pause_card_view(user_id: int, session_id: str) -> None:
    """Mark the live card paused so session updates buffer instead of
    rendering. Called when the user opens a Menu / sub-screen on the
    card's message — otherwise a stream of tool calls would overwrite
    whatever they're looking at."""
    state = _cards.get((user_id, session_id))
    if state is None:
        logger.info(
            "card_pause skip user=%d sess=%s reason=no_state",
            user_id,
            session_id,
            extra={
                "event": "card_pause_skip",
                "user_id": user_id,
                "session_id": session_id,
                "reason": "no_state",
            },
        )
        return
    state.in_menu_view = True
    logger.info(
        "card_pause user=%d sess=%s msg_id=%s lines=%d",
        user_id,
        session_id,
        state.msg_id,
        len(state.lines),
        extra={
            "event": "card_pause",
            "user_id": user_id,
            "session_id": session_id,
            "msg_id": state.msg_id,
            "lines": len(state.lines),
        },
    )


def transfer_card_to_carrier(
    user_id: int,
    from_session_id: str | None,
    to_session_id: str,
    target_message_id: int,
) -> None:
    """Hand off ownership of ``target_message_id`` from one session's
    live card to another's. Called when the switcher flips active.

    Effect:
      - FROM session is paused (``in_menu_view=True``) so its events
        buffer silently in ``state.lines`` instead of editing the
        carrier (which now belongs to the TO session). No new chat
        message lands until the user switches back or types text.
      - TO session claims the carrier (``msg_id=target_message_id``)
        and its pause is released, so the next event for it renders
        on the carrier — overlaying the preview that the callback
        just painted.

    No-op when ``from_session_id == to_session_id`` (user tapped the
    already-active session). The previous live-card behaviour — where
    A's lingering ``msg_id`` clobbered B's preview every time A emitted
    a tool call — falls out naturally because A is now paused.
    """
    if from_session_id == to_session_id:
        logger.info(
            "card_transfer skip user=%d sess=%s reason=same_session",
            user_id,
            to_session_id,
            extra={
                "event": "card_transfer_skip",
                "user_id": user_id,
                "session_id": to_session_id,
                "reason": "same_session",
            },
        )
        return
    from_msg_id_was: int | None = None
    if from_session_id:
        from_state = _cards.get((user_id, from_session_id))
        if from_state is not None:
            from_msg_id_was = from_state.msg_id
            if (
                from_state.pending_edit is not None
                and not from_state.pending_edit.done()
            ):
                from_state.pending_edit.cancel()
            from_state.pending_edit = None
            from_state.in_menu_view = True
    to_state = _cards.setdefault((user_id, to_session_id), CardState())
    to_msg_id_was = to_state.msg_id
    if to_state.pending_edit is not None and not to_state.pending_edit.done():
        to_state.pending_edit.cancel()
    to_state.pending_edit = None
    to_state.msg_id = target_message_id
    to_state.in_menu_view = False
    logger.info(
        "card_transfer user=%d from=%s (from_msg=%s) to=%s (was_msg=%s) carrier=%s",
        user_id,
        from_session_id or "-",
        from_msg_id_was,
        to_session_id,
        to_msg_id_was,
        target_message_id,
        extra={
            "event": "card_transfer",
            "user_id": user_id,
            "from_session_id": from_session_id,
            "from_msg_id_was": from_msg_id_was,
            "to_session_id": to_session_id,
            "to_msg_id_was": to_msg_id_was,
            "carrier_msg_id": target_message_id,
        },
    )


def detach_paused_cards_at_message(user_id: int, message_id: int) -> None:
    """Release card state bound to ``message_id`` when the carrier has
    been repurposed for a different flow.

    The pause→resume design assumes the user eventually returns to the
    live card via ``resume_card_view`` (typing text, etc.). But when
    the carrier message gets hijacked for a different session — e.g.
    user navigates ``Menu → ＋ new`` and confirms a directory, the new
    session's "Created" status now owns the message — the OLD session's
    pause never gets released and its events buffer forever. Worse,
    ``state.msg_id`` still points at a message that's no longer its
    card, so a later edit would clobber whatever's there.

    This helper resets ``msg_id`` (so the next event opens a fresh
    card) and clears the pause flags for every card on this user that
    happened to be paused on the now-stolen message.
    """
    detached: list[str] = []
    for (uid, sid), state in list(_cards.items()):
        if uid != user_id or state.msg_id != message_id:
            continue
        if state.pending_edit is not None and not state.pending_edit.done():
            state.pending_edit.cancel()
        state.pending_edit = None
        state.msg_id = None
        state.in_menu_view = False
        state.pending_complete_footer = None
        # Mark continuation so the next card visually flags carry-over
        # (``…continued`` in the header).
        state.is_continuation = True
        detached.append(sid)
    if detached:
        logger.info(
            "card_detach user=%d msg=%s sessions=%s",
            user_id,
            message_id,
            detached,
            extra={
                "event": "card_detach",
                "user_id": user_id,
                "msg_id": message_id,
                "sessions": detached,
            },
        )


async def resume_card_view(bot: Bot, user_id: int, sess: Session) -> None:
    """Re-render the card with whatever events accumulated while paused,
    then drop the pause. If a finalize_task fired during the pause, the
    catch-up render carries the ``(task complete)`` footer and resets
    the card afterward exactly as the live path would have."""
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        return
    state.in_menu_view = False
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    footer = state.pending_complete_footer or ""
    text = _render_card(sess, state, footer=footer, user_id=user_id)
    is_busy = footer == ""
    keyboard = build_footer_keyboard(user_id, screen="main", is_busy=is_busy)
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
            reply_markup=keyboard,
        )
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    except Exception as e:
        logger.debug("resume_card_view edit failed: %s", e)
    if state.pending_complete_footer:
        reset_card(user_id, sess.id)


async def clear_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Wipe the live card's body in response to a user-driven /clear.

    Edits the existing message to a header-only "(cleared)" snapshot
    so the user sees the previous tool log disappear, then drops the
    cached state so the next claude event spawns a fresh card.
    No-op when there is no live card.
    """
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        reset_card(user_id, sess.id)
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.lines = []
    state.last_rendered = ""
    text = _render_card(sess, state, footer="(cleared)", user_id=user_id)
    cleared_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
            reply_markup=cleared_kb,
        )
    except Exception as e:
        logger.debug("clear_card edit failed: %s", e)
    reset_card(user_id, sess.id)


def _card_is_busy(state: CardState) -> bool:
    """Is this card actually producing output right now? Drives the
    Stop ↔ Kill keyboard split.

    Naively keying off ``msg_id`` (card is alive) gave a false positive
    on sessions the user switched into but hadn't started working in —
    the carrier transfer leaves ``msg_id`` set even though no work is
    in flight, so Stop hung around until the next finalize. Naively
    keying off ``status_text`` (the tmux spinner) was the original
    behaviour and flickered Stop ↔ Kill between tool_use and
    tool_result.

    Compromise: ``msg_id`` AND (spinner OR recent event). ``recent``
    means within the last ``2 × CARD_EDIT_LAG`` seconds — long enough
    to bridge the ~100-500 ms gap between a tool returning and the
    next status update, short enough that an idle card flips to Kill
    within a couple of seconds.
    """
    if state.msg_id is None:
        return False
    if state.status_text:
        return True
    if state.last_event_ts <= 0:
        return False
    grace = max(2.0, config.card_edit_lag * 2)
    return (time.time() - state.last_event_ts) < grace


async def _send_card(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a brand-new card message and remember it as the live card.

    ``reply_markup`` overrides the default footer keyboard. Used by
    ``finalize_task`` to attach the idle-state Kill row to a completed
    result instead of the busy-state Stop row.
    """
    if reply_markup is None:
        # Default: a fresh card is being sent because a turn is in
        # flight (update_session_card, repost_card, continuation
        # overflow). ``_card_is_busy`` keys off ``state.msg_id`` which
        # is still None at this point — the right signal here is
        # "we're sending a card", which by definition means Stop is
        # the user's intent. ``finalize_task`` overrides ``reply_markup``
        # explicitly with the Kill keyboard when a turn completes.
        reply_markup = build_footer_keyboard(user_id, screen="main", is_busy=True)
    keyboard = reply_markup
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
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit the live card. Returns False if the edit failed permanently.

    Always sends a keyboard along with the text — relying on Telegram's
    "preserve keyboard when reply_markup is omitted" semantics turned out
    flaky (the buttons flickered between edits). Caller may pass an
    explicit `reply_markup`; otherwise we rebuild from current busy state.
    """
    if state.msg_id is None:
        return False
    # User is currently looking at a Menu / sub-screen on this card's
    # message. Don't repaint — would clobber whatever they're navigating.
    # State.lines keeps accumulating; resume_card_view will catch up.
    if state.in_menu_view:
        return True
    if reply_markup is None:
        reply_markup = build_footer_keyboard(
            user_id, screen="main", is_busy=_card_is_busy(state)
        )
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
            reply_markup=reply_markup,
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


async def _deferred_edit(
    bot: Bot, user_id: int, sess: Session, state: CardState, delay: float
) -> None:
    """Sleep `delay` then render the latest card state and edit once.

    The deferred task always picks up the latest `state.lines` / `status_text`,
    so multiple events arriving during the sleep collapse into a single edit.
    """
    try:
        await asyncio.sleep(delay)
        # Stale guard: card may have been reset (finalize_task) while we slept.
        if state.msg_id is None:
            return
        text = _render_card(sess, state, user_id=user_id)
        if text == state.last_rendered:
            return
        if await _edit_card(bot, user_id, state, text=text):
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("deferred card edit failed: %s", e)
    finally:
        state.pending_edit = None


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Append `msg` to the session's live card, or open a new one if needed.

    Triggers a fresh card on long pause and on hard-limit overflow.
    """
    state = get_card_state(user_id, sess)
    msg_id_in = state.msg_id
    in_menu_view_in = state.in_menu_view

    # Fresh-content card → seed the head with a handful of transcript
    # entries from before the user's most recent message so the new
    # card carries prior-turn context instead of starting blank.
    # One-shot: triggers when ``state.lines`` is empty, regardless of
    # whether ``msg_id`` is set (it is set after a switcher tap that
    # claimed the carrier — but ``state.lines`` is still empty for a
    # fresh session). Skipped during menu-view buffering —
    # resume_card_view handles its own catch-up rendering.
    if not state.lines and not state.in_menu_view and config.card_prior_context > 0:
        try:
            seed = await _seed_prior_context_lines(sess)
        except Exception as e:
            logger.debug("seed_prior_context failed: %s", e)
            seed = []
        if seed:
            state.lines.extend(seed)

    new_line = _line_for_event(msg)
    # tool_result: replace the matching tool_use line in place.
    replaced = False
    if msg.content_type == "tool_result" and msg.tool_use_id:
        for i, ln in enumerate(state.lines):
            if ln.tool_use_id == msg.tool_use_id:
                state.lines[i] = new_line
                replaced = True
                break

    # User has the menu / a sub-screen open on this card's message.
    # Buffer the event into ``state.lines`` so resume_card_view can catch
    # up; do NOT trigger stale-card resets, overflow continuations, or
    # any rendering — those would clobber the user's current view.
    if state.in_menu_view:
        if not replaced:
            state.lines.append(new_line)
        state.last_event_ts = time.time()
        logger.info(
            "card_update buffered sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            msg_id_in,
            msg.content_type,
            len(state.lines),
            extra={
                "event": "card_update_buffered",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": msg_id_in,
                "content_type": msg.content_type,
                "lines": len(state.lines),
                "in_menu_view": in_menu_view_in,
            },
        )
        return

    # Trigger: long pause → fresh card.
    if _is_stale(state):
        state.msg_id = None
        state.lines = []
        state.is_continuation = True
        state.last_rendered = ""

    if not replaced:
        state.lines.append(new_line)

    state.last_event_ts = time.time()

    # Cap visible tool history per CARD_VISIBLE_TOOLS.
    _collapse_old_tools(state)

    # Trigger: card overflow → continuation card.
    overflow = _ensure_room(sess, state, user_id=user_id)
    if overflow:
        # Couldn't fit even one line — start a fresh card holding only the new line.
        state.msg_id = None
        state.lines = [new_line]
        state.is_continuation = True
        state.last_rendered = ""

    text = _render_card(sess, state, user_id=user_id)

    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text)
        state.last_edit_ts = time.monotonic()
        logger.info(
            "card_update sent sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            state.msg_id,
            msg.content_type,
            len(state.lines),
            extra={
                "event": "card_update_sent",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": state.msg_id,
                "content_type": msg.content_type,
                "lines": len(state.lines),
            },
        )
        return

    # Coalesce edits — at most one editMessageText per live_lag seconds.
    # User setting takes precedence over the env-var default.
    user_lag = session_manager.get_user_settings(user_id).get("live_lag")
    if user_lag is None:
        user_lag = config.card_edit_lag
    lag = max(0.0, float(user_lag))
    elapsed = time.monotonic() - state.last_edit_ts if state.last_edit_ts else lag
    if lag <= 0 or elapsed >= lag:
        edited = await _edit_card(bot, user_id, state, text=text)
        if edited:
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            logger.info(
                "card_update edited sess=%s msg_id=%s ctype=%s lines=%d",
                sess.id,
                state.msg_id,
                msg.content_type,
                len(state.lines),
                extra={
                    "event": "card_update_edited",
                    "user_id": user_id,
                    "session_id": sess.id,
                    "msg_id": state.msg_id,
                    "content_type": msg.content_type,
                    "lines": len(state.lines),
                },
            )
        else:
            state.msg_id = None
            await _send_card(bot, user_id, sess, state, text=text)
            state.last_edit_ts = time.monotonic()
        return

    # Inside the coalescing window: ensure exactly one deferred edit is queued.
    if state.pending_edit is None or state.pending_edit.done():
        delay = max(0.05, lag - elapsed)
        state.pending_edit = asyncio.create_task(
            _deferred_edit(bot, user_id, sess, state, delay)
        )


_RESULT_DIVIDER = "\n────── Результат ──────"


async def finalize_task(bot: Bot, user_id: int, sess: Session, final_text: str) -> None:
    """Append the final assistant answer to the current live card.

    Earlier this method *replaced* the card body with the final answer
    and dropped tool history. The new behaviour keeps the tool log
    visible and appends the final text after a "Результат" divider so
    the user sees the journey + result in a single message. Long
    answers still spill into continuation cards via ``split_message``
    to respect Telegram's 4096-char limit. ``_ensure_room`` trims
    oldest tool lines if the divider+answer combo wouldn't fit.
    """
    state = get_card_state(user_id, sess)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None

    cleaned = (final_text or "").strip()
    if not cleaned:
        # No final text from Claude (e.g. /clear with nothing else).
        # Drop the card; no push — the previous "completion push"
        # behaviour was removed when the result moved into the card body.
        if state.in_menu_view:
            state.pending_complete_footer = "(task complete)"
            return
        reset_card(user_id, sess.id)
        return

    formatted = split_overflow(cleaned)
    cleaned = formatted.text
    attachments = formatted.attachments

    has_history = bool(state.lines)

    # User is on a Menu view — accumulate divider + answer into state and
    # mark the card "completion-pending". resume_card_view will render
    # the footer and reset. Attachments still go out (file delivery
    # shouldn't wait on a UI navigation).
    if state.in_menu_view:
        if has_history:
            state.lines.append(CardLine(text=_RESULT_DIVIDER))
        state.lines.append(CardLine(text=cleaned))
        state.last_event_ts = time.time()
        state.pending_complete_footer = "(task complete)"
        if attachments:
            await _send_attachments(bot, user_id, attachments)
        return

    if has_history:
        state.lines.append(CardLine(text=_RESULT_DIVIDER))

    # Body budget for the answer chunk after header + tool history + divider.
    overhead = len(_render_card(sess, state, footer="(task complete)", user_id=user_id))
    body_budget = max(200, CARD_HARD_LIMIT - overhead - 1)
    chunks = split_message(cleaned, max_length=body_budget)

    state.lines.append(CardLine(text=chunks[0]))
    state.last_event_ts = time.time()

    # If the combined card still overflows, _ensure_room trims old tool
    # lines from the front; if even that fails, open a continuation card
    # holding only divider + answer.
    overflow = _ensure_room(sess, state, user_id=user_id)
    if overflow:
        state.msg_id = None
        state.lines = (
            [CardLine(text=_RESULT_DIVIDER), CardLine(text=chunks[0])]
            if has_history
            else [CardLine(text=chunks[0])]
        )
        state.is_continuation = True
        state.last_rendered = ""

    # Finalised card → ``is_busy=False`` keyboard so the user sees Kill,
    # not Stop, on a completed result.
    done_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)

    text = _render_card(sess, state, footer="(task complete)", user_id=user_id)
    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text, reply_markup=done_kb)
    else:
        if await _edit_card(bot, user_id, state, text=text, reply_markup=done_kb):
            state.last_rendered = text
    state.last_edit_ts = time.monotonic()

    # Remaining chunks → fresh continuation cards (answer-only).
    for chunk in chunks[1:]:
        state.msg_id = None
        state.lines = [CardLine(text=chunk)]
        state.is_continuation = True
        state.last_rendered = ""
        text = _render_card(sess, state, footer="(task complete)", user_id=user_id)
        await _send_card(bot, user_id, sess, state, text=text, reply_markup=done_kb)
        state.last_edit_ts = time.monotonic()

    if attachments:
        await _send_attachments(bot, user_id, attachments)

    # No separate "✓ done" push under the card — the result is already
    # rendered inside the card with a "(task complete)" footer, and an
    # extra message just doubles the noise. Errors and AskUserQuestion
    # still emit pushes (those need to ping the user).
    reset_card(user_id, sess.id)


async def _send_attachments(
    bot: Bot, user_id: int, attachments: list[Attachment]
) -> None:
    """Send extracted overflow content. ``kind="photo"`` table extracts
    are rasterised via ``screenshot.text_to_image`` so wide tables land
    as inline images rather than `.md` files; everything else (oversized
    code blocks) goes through ``send_document`` as before.
    """
    import io as _io

    from ..screenshot import text_to_image
    from .tg_format import pretty_pad_table

    for att in attachments:
        try:
            if att.kind == "photo":
                source = att.content.decode("utf-8", errors="replace")
                rendered = pretty_pad_table(source)
                png = await text_to_image(rendered, with_ansi=False)
                await bot.send_photo(
                    chat_id=user_id,
                    photo=_io.BytesIO(png),
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=_io.BytesIO(att.content),
                    filename=att.filename,
                )
        except Exception as e:
            logger.debug("attachment %s send failed: %s", att.filename, e)


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
    # Migrate the switcher onto the latest pushed message. The keyboard's
    # busy state mirrors whether the session has a live card right now —
    # finalize_task / completion-style pushes happen after reset_card so
    # the live state is gone, the user is at idle and should see Kill.
    if sent is not None:
        _register_msg(user_id, sent.message_id, sess.id)
        live = _cards.get((user_id, sess.id))
        is_busy = live is not None and live.msg_id is not None
        keyboard: InlineKeyboardMarkup | None = build_footer_keyboard(
            user_id, screen="main", is_busy=is_busy
        )
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


def is_card_busy(user_id: int, session_id: str) -> bool:
    """True when the user's live card for ``session_id`` is currently in
    flight AND visible (msg_id set, finalize_task hasn't run, and the
    card is not paused for menu navigation). Used by the polling-based
    typing-indicator path — TYPING should fire while a turn is mid-
    stream during silent gaps between events, but NOT while the user
    is browsing the inline ⋯ Menu / sub-screens. While ``in_menu_view``
    the card buffers events without rendering to chat, so a "typing…"
    indicator there is just noise.
    """
    state = _cards.get((user_id, session_id))
    if state is None or state.in_menu_view:
        return False
    return _card_is_busy(state)


def is_active_for_user(user_id: int, sess: Session) -> bool:
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


async def repost_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Resend the active live card as a fresh message and drop the old one.

    Used to keep the card visually below the user's latest message when
    ``card_position`` is set to ``repost``. No-op when the session has no
    live card or the card is paused (menu / sub-screen open).
    """
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None or state.in_menu_view:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
        state.pending_edit = None
    old_msg_id = state.msg_id
    state.msg_id = None  # force _send_card to create a fresh message
    text = _render_card(sess, state, user_id=user_id)
    await _send_card(bot, user_id, sess, state, text=text)
    state.last_rendered = text
    state.last_edit_ts = time.monotonic()
    if state.msg_id and state.msg_id != old_msg_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
        except Exception as e:
            logger.debug("repost_card delete old failed: %s", e)


async def refresh_panel(bot: Bot, user_id: int) -> None:
    """Re-render the active session's live card so the bg-status panel
    (and active quota glyph) reflects the latest bg_status state.

    No-op when:
      - the user has no active session
      - the active session has no live card yet
      - the card is paused (menu/sub-screen open)
      - a deferred edit is already queued — that edit will pick up the
        latest panel state on its own when it fires
    """
    active = session_manager.get_active_session(user_id)
    if active is None:
        return
    state = _cards.get((user_id, active.id))
    if state is None or state.msg_id is None or state.in_menu_view:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        return
    text = _render_card(active, state, user_id=user_id)
    if text == state.last_rendered:
        return
    if await _edit_card(bot, user_id, state, text=text):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()


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
    was_busy = bool(state.status_text)
    state.status_text = status_text
    is_busy = bool(state.status_text)
    rendered = _render_card(sess, state, user_id=user_id)
    if rendered == state.last_rendered and was_busy == is_busy:
        return

    # When busy state flips, refresh the keyboard so Stop ↔ Kill swaps.
    keyboard = (
        build_footer_keyboard(user_id, screen="main", is_busy=is_busy)
        if was_busy != is_busy
        else None
    )
    if await _edit_card(bot, user_id, state, text=rendered, reply_markup=keyboard):
        state.last_rendered = rendered
