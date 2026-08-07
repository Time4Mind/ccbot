"""Keep silent unfinished turns observable through their live terminal pane."""

from __future__ import annotations

import logging
import time

from telegram import Bot

from ..session import Session, session_manager
from . import bg_status
from .card_binding import clear_carrier, restore_carrier, snapshot_carrier
from .card_model import (
    _card_is_busy,
)
from .card_types import TurnPhase

from .card_registry import (
    _cards,
    _card_lock,
    _legacy,
)
from .card_seed import get_card_state

logger = logging.getLogger(__name__)


__all__ = [
    "is_card_in_menu_view",
    "is_card_finalized",
    "is_card_busy",
    "STALL_FINALIZE_AFTER_SECONDS",
    "STALL_FINALIZE_TOOL_USE_SECONDS",
    "maybe_finalize_stalled",
    "is_active_for_user",
    "repost_card",
]


def is_card_in_menu_view(user_id: int, session_id: str) -> bool:
    """True if the user is currently browsing a Menu / sub-screen on
    this session's card. Used by ``status_polling`` to gate the TYPING
    indicator — firing it while the user navigates menus is just noise.
    """
    state = _cards.get((user_id, session_id))
    return state is not None and state.in_menu_view


def is_card_finalized(user_id: int, session_id: str) -> bool:
    """True when the card's tail event is a terminal one (``final_text``
    or ``error``). Used by ``status_polling`` to suppress a stale pane
    spinner (e.g. ``Sautéed for 11m 16s · 1 shell still running``) that
    persists in scrollback after end-of-turn — without this check the
    typing indicator stays on forever waiting for the user-visible
    spinner string to scroll off.
    """
    state = _cards.get((user_id, session_id))
    if state is None or not state.events:
        return False
    return state.events[-1].type in ("final_text", "error")


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


# ─── Silent unfinished-turn observation ──────────────────────────────
# A silent turn must not be guessed complete. Once its conservative idle
# threshold elapses, keep the active card RUNNING and periodically refresh its
# terminal pane. The user can then see whether the process resumes or changes
# without receiving a synthetic final answer or a separate warning push.

# How long an active card may sit with a non-terminal tail event and an
# idle (non-busy) pane before we declare it stalled and finalise it.
# Deliberately generous: a genuinely-busy claude keeps the pane spinner
# *changing* (``Working… (17s)`` → ``(18s)`` → …) so ``pane_busy`` stays
# True and this never fires during long thinking / a slow tool. We only
# trip when the spinner is gone or frozen AND no new renderable event
# arrived for this long — i.e. the subprocess produced nothing.
#
# Two-tier threshold by tail event type. Tools legitimately run for
# minutes (slow Bash, CHYT, Map-Reduce, network) and Claude routinely
# spends a comparable amount of time reasoning after the last tool
# result before emitting the final assistant turn — both produce a
# silent JSONL tail of ``tool_use``. Pre-textual silence (``text`` /
# ``thinking`` tail) is rarer and more suspicious because Claude is
# mid-emit, so the original threshold still applies there.
STALL_FINALIZE_AFTER_SECONDS = 90.0
STALL_FINALIZE_TOOL_USE_SECONDS = 300.0

_STALL_PANE_REFRESH_SECONDS = 3.0


async def maybe_finalize_stalled(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    pane_busy: bool,
    interactive_waiting: bool,
    in_menu: bool,
    now: float | None = None,
) -> bool:
    """Keep an active silent turn RUNNING and refresh only its live pane.

    Fires (returns True after finalising) ONLY when ALL hold:

      * a card exists for this (user, session) with at least one event;
      * the card is NOT already finalized (tail event is non-terminal —
        mid ``thinking`` / ``tool_use`` / ``text``);
      * the pane spinner is NOT busy (``pane_busy=False`` — gone or
        frozen, per ``_pane_status_is_changing``);
      * no interactive UI is waiting for the user (``interactive_waiting``
        — AskUserQuestion / ExitPlanMode / Permission / RestoreCheckpoint,
        or kb-mode) and the card is not in a Menu sub-screen
        (``in_menu``);
      * no new renderable event arrived for ``STALL_FINALIZE_AFTER_SECONDS``
        (measured from ``state.last_event_ts``).

    The last condition is what keeps this conservative: a long-thinking
    turn keeps the pane spinner changing, so ``pane_busy`` is True and we
    bail; a tool_use legitimately awaiting a slow result either keeps the
    spinner alive or lands its result well before the window elapses. We
    only trip when the spinner has died AND the transcript stopped
    growing — the exact fingerprint of a stalled / exited subprocess.

    No final event and no Telegram push are produced. An active session keeps
    its pane visible; a background session gets only the ``stalled`` status
    marker used by the background-session panel.
    """
    state = _cards.get((user_id, sess.id))
    active = is_active_for_user(user_id, sess)
    if state is not None and pane_busy and state.stall_watch_active:
        state.stall_watch_active = False
        state.last_stall_pane_refresh_ts = 0.0
        if not active and bg_status.update_status(user_id, sess.id, "working"):
            await _legacy("refresh_panel")(bot, user_id)
    if pane_busy or interactive_waiting or (in_menu and active):
        return False
    if state is None or not state.events:
        return False
    if (active and state.in_menu_view) or state.in_kb_mode:
        return False
    # Already finalized — nothing frozen to rescue.
    tail_type = state.events[-1].type
    if tail_type in ("final_text", "error"):
        return False
    if state.last_event_ts <= 0:
        return False
    when = now if now is not None else time.time()
    threshold = (
        STALL_FINALIZE_TOOL_USE_SECONDS
        if tail_type == "tool_use"
        else STALL_FINALIZE_AFTER_SECONDS
    )
    if (when - state.last_event_ts) < threshold:
        return False
    if not state.stall_watch_active:
        logger.warning(
            "stall_watch user=%d sess=%s wid=%s idle=%.0fs tail=%s threshold=%.0fs",
            user_id,
            sess.id,
            sess.window_id,
            when - state.last_event_ts,
            tail_type,
            threshold,
        )
    state.stall_watch_active = True
    state.turn_phase = TurnPhase.RUNNING
    if not active:
        if bg_status.update_status(user_id, sess.id, "stalled"):
            await _legacy("refresh_panel")(bot, user_id)
        return True
    if state.msg_id is None:
        return False
    refresh_now = time.monotonic()
    if refresh_now - state.last_stall_pane_refresh_ts >= _STALL_PANE_REFRESH_SECONDS:
        text = _legacy("_render_card")(sess, state, user_id=user_id)
        if await _legacy("_edit_card")(bot, user_id, state, text=text):
            state.last_rendered = text
            state.last_edit_ts = refresh_now
        state.last_stall_pane_refresh_ts = refresh_now
    return True


def is_active_for_user(user_id: int, sess: Session) -> bool:
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


async def repost_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Send a fresh live-card below the user's latest message, and drop
    the previous one if it exists.

    Called from text_handler on every user-msg dispatch (the legacy
    ``card_position`` setting was retired; always-repost is now the
    single canonical behaviour). The user always sees a bot-side card
    immediately below the message they just typed instead of having
    to wait for claude's first event — which may come seconds later
    when the model spends a while in thinking before any tool call.

    No-op only when the card is paused (Menu / sub-screen open). In all
    other cases — including the post-finalize_task state where the
    previous live card was already pinned + reset — we seed a fresh
    card so it lands below the user's typed line. When claude's first
    event arrives it will edit *this* card (state.msg_id is now set)
    instead of spawning a separate one above the user msg.
    """
    state = _cards.get((user_id, sess.id))
    if state is not None and state.in_menu_view:
        return
    state = get_card_state(user_id, sess)
    # Seed history from JSONL on first call after a bot restart so the
    # reposted card lands with full context, not an empty body.
    await _legacy("_ensure_seeded")(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
        state.pending_edit = None

    # Lock the msg_id mutation + spawn so a parallel
    # ``update_session_card`` (for a claude event arriving mid-typing)
    # can't see the brief ``msg_id is None`` window and spawn its own
    # card too — Task #50.
    async with _card_lock(user_id, sess.id):
        old_binding = snapshot_carrier(state)
        old_msg_id = clear_carrier(state)  # force a fresh Telegram message

        text = _legacy("_render_card")(sess, state, user_id=user_id)
        sent = await _legacy("_send_card")(bot, user_id, sess, state, text=text)
        if sent is False or state.msg_id is None:
            restore_carrier(state, old_binding)
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        # A freshly (re)posted card is brand new — reset the freshness
        # clock so the first arriving claude event can't misjudge it as
        # stale and spawn a SECOND card ~1-2s later (the delete+resend
        # flicker). ``repost_card`` previously updated last_rendered /
        # last_edit_ts but left ``last_event_ts`` pinned to the previous
        # turn; on a card idle >= STALE_CARD_SECONDS that tripped
        # ``_is_stale`` on the very next event. A repost is itself user
        # activity, so "now" is the correct freshness stamp.
        state.last_event_ts = time.time()
        new_msg_id = state.msg_id
    logger.info(
        "repost_card user=%s sess=%s old_msg=%s new_msg=%s events=%d",
        user_id,
        sess.id,
        old_msg_id,
        new_msg_id,
        len(state.events),
    )
    if old_msg_id and new_msg_id and new_msg_id != old_msg_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
            logger.info(
                "repost_card deleted_old user=%s sess=%s msg=%s",
                user_id,
                sess.id,
                old_msg_id,
            )
        except Exception as e:
            logger.warning(
                "repost_card delete_old_failed user=%s sess=%s msg=%s err=%s",
                user_id,
                sess.id,
                old_msg_id,
                e,
            )
