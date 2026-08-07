"""Schedule receipt surfaces, panel refreshes, shutdown, and timer ticks."""

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

from ..session import Session, session_manager
from .card_binding import clear_carrier, restore_carrier, snapshot_carrier
from .card_model import (
    _latest_inflight_idx,
    _resolved_page_idx,
    paginate_events_for_card,
)

from .card_registry import (
    _cards,
    _card_surface_tasks,
    _card_lock,
    _carrier_edit_lock,
    _legacy,
)
from .card_seed import get_card_state

logger = logging.getLogger(__name__)


__all__ = [
    "surface_card_after_message",
    "schedule_card_after_message",
    "shutdown_card_surface_tasks",
    "refresh_panel",
    "CARD_TIMER_TICK_SECONDS",
    "card_timer_loop",
]


async def surface_card_after_message(
    bot: Bot,
    user_id: int,
    sess: Session,
    message_id: int,
) -> bool:
    """Make the active session card the only receipt for an inbound message.

    Telegram message ids are monotonic within a chat.  The card therefore
    acknowledges queue admission simply by sitting below ``message_id``.  The
    position check and repost are serialized so several messages arriving in
    one burst converge on one newest card instead of spawning one per task.
    """
    state = get_card_state(user_id, sess)
    await _legacy("_ensure_seeded")(user_id, sess, state)
    old_msg_id: int | None = None
    new_msg_id: int | None = None

    async with _card_lock(user_id, sess.id):
        async with _carrier_edit_lock(user_id):
            if not _legacy("is_active_for_user")(user_id, sess):
                return False
            if state.msg_id is not None and state.msg_id > message_id:
                return True

            if state.pending_edit is not None and not state.pending_edit.done():
                state.pending_edit.cancel()
            state.pending_edit = None
            # Sending content is an explicit return from a menu/sub-screen to
            # the live conversation.  Otherwise the old menu card would stay
            # above the message and acceptance would remain invisible.
            state.in_menu_view = False
            old_binding = snapshot_carrier(state)
            old_msg_id = clear_carrier(state)
            text = _legacy("_render_card")(sess, state, user_id=user_id)
            await _legacy("_send_card")(bot, user_id, sess, state, text=text)
            new_msg_id = state.msg_id
            if new_msg_id is None:
                # Telegram send failed: retain the existing carrier binding so
                # later events can recover instead of orphaning a valid card.
                restore_carrier(state, old_binding)
                return False
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            state.last_event_ts = time.time()

    logger.info(
        "card_surface user=%s sess=%s after=%s old_msg=%s new_msg=%s",
        user_id,
        sess.id,
        message_id,
        old_msg_id,
        new_msg_id,
    )
    if old_msg_id and new_msg_id != old_msg_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
        except Exception as exc:
            logger.warning(
                "card_surface delete_old_failed user=%s sess=%s msg=%s err=%s",
                user_id,
                sess.id,
                old_msg_id,
                exc,
            )
    return True


def schedule_card_after_message(
    bot: Bot,
    user_id: int,
    sess: Session,
    message_id: int,
) -> asyncio.Task[bool]:
    """Schedule a non-blocking card receipt for Telegram intake."""
    task = asyncio.create_task(
        surface_card_after_message(bot, user_id, sess, message_id),
        name=f"card-surface:{user_id}:{sess.id}:{message_id}",
    )
    _card_surface_tasks.add(task)

    def _finished(done: asyncio.Task[bool]) -> None:
        _card_surface_tasks.discard(done)
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            logger.warning(
                "card_surface failed user=%s sess=%s after=%s err=%s",
                user_id,
                sess.id,
                message_id,
                exc,
            )

    task.add_done_callback(_finished)
    return task


async def shutdown_card_surface_tasks() -> None:
    """Cancel outstanding intake card moves during application shutdown."""
    tasks = list(_card_surface_tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _card_surface_tasks.clear()


async def refresh_panel(bot: Bot, user_id: int, *, immediate: bool = False) -> bool:
    """Re-render the active session's live card so the bg-status panel
    (and active quota glyph) reflects the latest bg_status state.

    No-op when:
      - the user has no active session
      - the active session has no live card yet
      - the card is paused (menu/sub-screen open)
      - a deferred edit is already queued, unless ``immediate`` is True

    Interactive actions such as pagination pass ``immediate=True``. That
    cancels the live-update debounce and paints the requested page now,
    instead of making the button appear stuck until ``live_lag`` expires.
    """
    active = session_manager.get_active_session(user_id)
    if active is None:
        return False
    state = _cards.get((user_id, active.id))
    if state is None or state.msg_id is None or state.in_menu_view:
        return False
    original_msg_id = state.msg_id
    if state.pending_edit is not None and not state.pending_edit.done():
        if not immediate:
            return True
        pending = state.pending_edit
        if not state.pending_edit_in_flight:
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        state.pending_edit = None
        # The awaited task may have yielded long enough for a carrier or
        # active-session hand-off. Never paint the old page onto a new owner.
        current_active = session_manager.get_active_session(user_id)
        if (
            current_active is None
            or current_active.id != active.id
            or _cards.get((user_id, active.id)) is not state
            or state.msg_id != original_msg_id
            or state.in_menu_view
        ):
            return False
    text = _legacy("_render_card")(active, state, user_id=user_id)
    if text == state.last_rendered:
        return True
    if await _legacy("_edit_card")(
        bot,
        user_id,
        state,
        text=text,
        refresh_pane=not immediate,
    ):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        return True
    return False


# ─── Tool-timer tick ──────────────────────────────────────────────────

# How often to re-render the active card to advance the ⏳ M:SS counter
# on the latest in-flight tool/thinking entry. Matches the
# session_monitor poll cadence (2 s) so the card feels as responsive as
# Telegram's own "typing…" indicator — per-user feedback on pivot #39.
# Inline-screenshot cards are additionally throttled by
# ``_PHOTO_EDIT_MIN_INTERVAL`` (2.5 s) so editMessageMedia bursts stay
# within Telegram's limits.
CARD_TIMER_TICK_SECONDS = 2.0


async def card_timer_loop(bot: Bot) -> None:
    """Background task that ticks the elapsed timer on the latest
    in-flight tool/thinking entry of each user's active card.

    Skips:
      - cards with no msg_id
      - paused cards (in_menu_view)
      - users whose pagination puts them on a non-latest page (timer
        only ticks on the page where the in-flight event lives, i.e.
        the latest page)
      - cards with a pending deferred edit (the deferred edit picks up
        the updated timer when it fires)
    """
    logger.info("card_timer_loop started tick=%.1fs", CARD_TIMER_TICK_SECONDS)
    while True:
        try:
            await asyncio.sleep(CARD_TIMER_TICK_SECONDS)
            for (uid, sid), state in list(_cards.items()):
                try:
                    if state.msg_id is None or state.in_menu_view:
                        continue
                    sess = session_manager.get_session(sid)
                    if sess is None:
                        continue
                    # Only the user's currently-active session ticks.
                    active = session_manager.get_active_session(uid)
                    if active is None or active.id != sid:
                        continue
                    pages = paginate_events_for_card(state, uid)
                    idx = _resolved_page_idx(state, len(pages))
                    # Timer renders only on the latest page.
                    if idx != len(pages) - 1:
                        continue
                    if _latest_inflight_idx(pages[idx]) is None:
                        continue
                    # Skip when an edit is already queued — it'll pick
                    # up the fresh timer value when it fires.
                    if state.pending_edit is not None and not state.pending_edit.done():
                        continue
                    text = _legacy("_render_card")(sess, state, user_id=uid)
                    if text == state.last_rendered:
                        continue
                    if await _legacy("_edit_card")(bot, uid, state, text=text):
                        state.last_rendered = text
                        state.last_edit_ts = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("card_timer tick failed for sess=%s: %s", sid, e)
        except asyncio.CancelledError:
            logger.info("card_timer_loop cancelled")
            break
        except Exception as e:
            logger.warning("card_timer_loop error: %s", e)
