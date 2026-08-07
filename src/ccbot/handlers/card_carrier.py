"""Transfer, pause, restore, clear, and resume Telegram card carriers."""

from __future__ import annotations

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

from ..session import Session, session_manager
from .card_model import (
    CardState,
    _card_is_busy,
)
from .card_registry import (
    _cards,
    _card_lock,
    _carrier_edit_lock,
    _strip_stale_switchers,
    _register_msg,
    reset_card,
    _legacy,
)

logger = logging.getLogger(__name__)


__all__ = [
    "_recover_from_false_stall",
    "cancel_pending_card_edits",
    "close_card_view",
    "set_card_context_pct",
    "mark_card_paused",
    "pause_card_view",
    "transfer_card_to_carrier",
    "activate_card_on_carrier",
    "card_is_below",
    "detach_paused_cards_at_message",
    "release_card_message",
    "resume_card_view",
    "paint_card_on_carrier",
    "restore_card",
    "clear_card",
]


def _recover_from_false_stall(state: CardState) -> None:
    """Wipe the live-card binding after a false-positive stall_finalize.

    Set when a genuine assistant turn lands AFTER
    ``maybe_finalize_stalled`` armed ``state.stall_finalized``. Clears
    msg_id / events / pagination so the next render path goes through
    ``_send_card`` (fresh message below the stalled stub) rather than
    ``_edit_card`` (silent edit of the now-finalized card). The stalled
    stub stays in chat history with its STALL_NOTE — we don't rewrite
    it; the recovery message appears as a fresh card with
    ``is_continuation=True`` so the header carries the ``…continued``
    marker.
    """
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = None
    state.events = []
    state.current_page_idx = None
    state.is_continuation = True
    state.last_rendered = ""
    state.seed_attempted = False
    state.seed_mtime = -1.0
    state.stall_finalized = False


async def cancel_pending_card_edits(timeout: float = 2.0) -> None:
    """Cancel + drain every deferred ``_edit_card`` task across all cards.

    Called from ``post_shutdown`` so we don't leave ``_deferred_edit``
    tasks in the "pending" state when the event loop closes — asyncio
    logs ``Task was destroyed but it is pending!`` for each one, and
    any in-flight Telegram edit can race with the final state save.
    """
    tasks: list[asyncio.Task[None]] = []
    for state in _cards.values():
        t = state.pending_edit
        if t is not None and not t.done():
            t.cancel()
            tasks.append(t)
        state.pending_edit = None
    if not tasks:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(
            "card-edit shutdown drain timed out after %ss with %d tasks pending",
            timeout,
            sum(1 for t in tasks if not t.done()),
        )


async def close_card_view(bot: Bot, user_id: int, session_id: str) -> None:
    """Release the live card slot so the next event creates a fresh
    message instead of editing the old carrier.

    Used by the Shot button (Task #51): the screenshot photo replaces
    the live card visually, and when the user comes back from the
    screenshot we want a NEW card message to appear (replacement of
    one message by another), not an in-place edit of a now-stale
    carrier far up the chat.

    Steps:
      - Cancel any pending edit on the old carrier.
      - **Delete** the old carrier message so the chat reads as a
        clean replacement (per #52 follow-up — stripping the keyboard
        was confusing, the orphaned message read like a frozen card).
      - Drop ``msg_id`` so the next claude event / Shot Back spawns a
        fresh card.
      - Leave ``in_menu_view=True`` so events buffer until the user
        actually navigates back (the Shot Back handler clears it).
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    old_msg_id = state.msg_id
    state.msg_id = None
    state.is_photo_msg = False
    state.last_rendered = ""
    state.last_pane_hash = ""
    state.last_photo_edit_ts = 0.0
    state.in_menu_view = True
    if old_msg_id is not None:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
        except Exception as e:
            logger.debug(
                "close_card_view: delete old msg failed msg_id=%s: %s",
                old_msg_id,
                e,
            )
    logger.info(
        "card_close user=%d sess=%s old_msg_id=%s",
        user_id,
        session_id,
        old_msg_id,
        extra={
            "event": "card_close",
            "user_id": user_id,
            "session_id": session_id,
            "old_msg_id": old_msg_id,
        },
    )


def set_card_context_pct(user_id: int, session_id: str, pct: int) -> None:
    """Stash the latest context-window fill percentage for this session's
    live card. Read by ``_render_card`` to paint a ``context: N%`` line
    above the bg-status panel. No-op when no state exists yet.
    """
    state = _cards.setdefault((user_id, session_id), CardState())
    state.context_pct = pct


def mark_card_paused(user_id: int, session_id: str) -> None:
    """Force a card to ``in_menu_view=True``, creating empty state if
    none exists. Differs from :func:`pause_card_view` which silently
    no-ops on a missing state — needed for the Shot → switcher path
    where the user pivots onto a session whose card was never seeded.
    """
    _cards.setdefault((user_id, session_id), CardState()).in_menu_view = True


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
        len(state.events),
        extra={
            "event": "card_pause",
            "user_id": user_id,
            "session_id": session_id,
            "msg_id": state.msg_id,
            "lines": len(state.events),
        },
    )


def transfer_card_to_carrier(
    user_id: int,
    from_session_id: str | None,
    to_session_id: str,
    target_message_id: int,
) -> int | None:
    """Hand off ownership of ``target_message_id`` from one session's
    live card to another's. Called when the switcher flips active.

    Returns the message id of the TO session's *previous* card when it
    was a different message — that message is now orphaned (nothing will
    ever edit it again) and the caller must strip its keyboard, or the
    chat ends up with two tappable switchers. Returns None when there is
    nothing to clean up.

    Effect:
      - FROM session is paused (``in_menu_view=True``) so its events
        buffer silently in ``state.events`` instead of editing the
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
        return None
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
    session_manager.set_card_msg(user_id, target_message_id)
    # Pause the TO card across the switch window. The caller (CB_SW_USE)
    # will paint history on this message_id next, and then call
    # ``release_card_message`` which clears both ``msg_id`` and
    # ``in_menu_view``. If we left ``in_menu_view=False`` here, any bg
    # event arriving in the ~150 ms parse + edit window would trigger
    # ``refresh_panel`` — that path sees
    # ``msg_id=carrier`` + ``in_menu_view=False`` and rerenders the
    # live-card body over the carrier, clobbering the history paint
    # we're racing to land. Symptom: user sees "header + bg panel"
    # instead of transcript after a switch.
    to_state.in_menu_view = True
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
    if to_msg_id_was is not None and to_msg_id_was != target_message_id:
        return to_msg_id_was
    return None


async def activate_card_on_carrier(
    user_id: int,
    from_session_id: str | None,
    to_session_id: str,
    target_message_id: int,
) -> int | None:
    """Atomically hand the live carrier to a newly-active session.

    The carrier-edit lock is a barrier against an edit from the previous
    active session that is already in flight.  Once the barrier opens, pause
    the old card, claim the carrier for the target, and flip ``active_sessions``
    before another card edit can start.  The caller paints the target after
    this returns; any queued old-session edit then sees the paused state and
    becomes a no-op.

    Returns the target session's orphaned previous card message id, matching
    :func:`transfer_card_to_carrier`.
    """
    async with _carrier_edit_lock(user_id):
        orphan_msg_id = transfer_card_to_carrier(
            user_id,
            from_session_id,
            to_session_id,
            target_message_id,
        )
        session_manager.set_active_session(user_id, to_session_id)
        return orphan_msg_id


def card_is_below(user_id: int, session_id: str, message_id: int) -> bool:
    """True when the session's live card already sits *below*
    ``message_id`` in the chat.

    Telegram message ids are monotonically increasing per chat, so a
    card whose ``msg_id`` is greater than the user's message was posted
    after it and is already "in front" — a repost would only churn.
    Used by the voice flow: the card is reposted at voice-receipt, so
    when whisper returns 30 s later there is nothing to move, just the
    🎙 marker to drop with an in-place edit.
    """
    state = _cards.get((user_id, session_id))
    return state is not None and state.msg_id is not None and state.msg_id > message_id


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


def release_card_message(user_id: int, session_id: str) -> None:
    """Drop the live-card binding to its current Telegram message_id
    without touching the message itself.

    Called from the switcher-tap handler right after history is painted
    on the carrier: the carrier now holds a frozen transcript view, and
    the TO session's live card must NOT keep editing it. With ``msg_id``
    cleared, the next claude event opens a fresh card below (carrying
    the bg-status panel and prior-context seed); the history carrier
    stays put and remains paginable.

    Buffered ``lines`` are also wiped — they were destined for the
    overwritten card; the fresh card starts empty on its next event.
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = None
    state.in_menu_view = False
    state.events = []
    state.last_rendered = ""
    state.is_continuation = True
    # A6: this is a non-destructive carrier hand-off — the session keeps
    # running with a full transcript. Allow the next event's fresh card
    # to re-seed so its footer page counter reflects the real recent
    # turn-history instead of collapsing to ``1/1``.
    state.seed_attempted = False
    state.seed_mtime = -1.0
    logger.info(
        "card_release user=%d sess=%s",
        user_id,
        session_id,
        extra={
            "event": "card_release",
            "user_id": user_id,
            "session_id": session_id,
        },
    )


async def resume_card_view(bot: Bot, user_id: int, sess: Session) -> None:
    """Drop the menu-pause so future events render again, and re-paint
    the carrier with the buffered events.

    For the currently-active session, clears ``in_menu_view`` even when
    ``msg_id`` was lost (carrier stale / deleted / not yet created). Earlier
    this returned early without clearing the pause, leaving the active card
    stuck in ``must_buffer=True`` forever. A background session is the one
    exception: its pause and carrier binding must remain untouched so a late
    voice dispatch cannot reclaim the newly-active session's carrier.
    """
    # ``setdefault`` so a session with no card-state yet (just-switched
    # bg session via Shot's switcher) still lands on a visible surface.
    # Without this, resume_card_view silently bailed and Back left the
    # user staring at empty chat.
    state = _cards.setdefault((user_id, sess.id), CardState())

    async def _spawn_fresh() -> None:
        await _legacy("_ensure_seeded")(user_id, sess, state)
        fresh_text = _legacy("_render_card")(sess, state, user_id=user_id)
        fresh_kb = _legacy("build_footer_keyboard")(
            user_id, screen="main", is_busy=True
        )
        await _legacy("_send_card")(
            bot, user_id, sess, state, text=fresh_text, reply_markup=fresh_kb
        )

    # Spawn-serialization (Task #50): hold the per-session lock across
    # the msg_id check + send/edit. Otherwise a claude event arriving
    # during ``_ensure_seeded`` / ``_send_card`` can race and produce a
    # duplicate card via ``update_session_card``.
    async with _card_lock(user_id, sess.id):
        # The active-session check and the Telegram edit share the same
        # cross-session barrier as switcher hand-off.  A slow voice dispatch
        # may have started while this session was active and resumed after the
        # carrier moved elsewhere; it must not clear the old card's pause or
        # repaint the new owner's carrier.
        async with _carrier_edit_lock(user_id):
            if not _legacy("is_active_for_user")(user_id, sess):
                logger.info(
                    "card_resume skip user=%d sess=%s reason=background",
                    user_id,
                    sess.id,
                )
                return
            state.in_menu_view = False
            if state.pending_edit is not None and not state.pending_edit.done():
                state.pending_edit.cancel()
            state.pending_edit = None
            if state.msg_id is None:
                # No carrier — spawn a fresh card now so the user lands on a
                # visible surface immediately (used by Shot → Back after #51's
                # ``close_card_view`` drops msg_id). Previously we waited for
                # the next claude event; on quiet sessions that left the user
                # staring at empty chat.
                await _spawn_fresh()
                return
            text = _legacy("_render_card")(sess, state, user_id=user_id)
            keyboard = _legacy("build_footer_keyboard")(
                user_id, screen="main", is_busy=True
            )
            if await _legacy("_edit_card_unlocked")(
                bot, user_id, state, text=text, reply_markup=keyboard
            ):
                state.last_rendered = text
                state.last_edit_ts = time.monotonic()
                return
            # ``_edit_card_unlocked`` returned False — the carrier was lost
            # (stale msg, already-deleted, or bot can't edit it) and already
            # reset msg_id internally. Spawn a fresh card so the user still
            # lands on a visible live surface.
            await _spawn_fresh()


async def paint_card_on_carrier(
    bot: Bot,
    user_id: int,
    sess: Session,
    carrier_msg_id: int,
) -> None:
    """Claim ``carrier_msg_id`` as ``sess``'s live card and paint it.

    Used by Menu → Sessions: the carrier is the menu message the user just
    tapped, and we want it to become the live card (one unified surface
    instead of a separate list rendering). The previous ``state.msg_id``
    is left as a frozen artifact in chat — the next claude event uses
    the new carrier.
    """
    state = _cards.setdefault((user_id, sess.id), CardState())
    # Menu → Sessions on a fresh post-restart state: seed history first
    # so the user lands on a card with their conversation, not 1/1.
    await _legacy("_ensure_seeded")(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = carrier_msg_id
    state.in_menu_view = False
    state.last_rendered = ""
    _register_msg(user_id, carrier_msg_id, sess.id)
    session_manager.set_card_msg(user_id, carrier_msg_id)
    text = _legacy("_render_card")(sess, state, user_id=user_id)
    keyboard = _legacy("build_footer_keyboard")(
        user_id, screen="main", is_busy=_card_is_busy(state)
    )
    if await _legacy("_edit_card")(
        bot, user_id, state, text=text, reply_markup=keyboard
    ):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        # Migrate the switcher pointer onto the new carrier so previous
        # switcher rows in chat stop being the canonical surface.
        await _strip_stale_switchers(bot, user_id, carrier_msg_id, sess.id)
        session_manager.set_last_switcher_msg(user_id, carrier_msg_id)


async def restore_card(bot: Bot, user_id: int, sess: Session, card_msg_id: int) -> bool:
    """Repaint a persisted live card in place after a bot restart.

    ``_cards`` is in-memory only, so a restart loses every live card's
    ``CardState`` and the chat is left with a frozen, orphaned card
    message. The card's ``message_id`` is persisted per active session
    (``session_manager.card_msg_id``); on startup we rebuild a fresh
    ``CardState``, seed the recent transcript from JSONL, and edit the
    original message in place so the live card resumes on the same
    message instead of a new one appearing on the next event.

    Returns True if the in-place edit landed. On failure (message
    deleted by the user, edit rejected) the stale pointer is cleared so
    the next claude event spawns a fresh card normally.
    """
    existing = _cards.get((user_id, sess.id))
    if existing is not None and existing.msg_id is not None:
        # A claude event already raced ahead and established a live card
        # for this session — leave it alone rather than fight it.
        return True
    state = _cards.setdefault((user_id, sess.id), CardState())
    state.msg_id = card_msg_id
    state.last_rendered = ""
    await _legacy("_ensure_seeded")(user_id, sess, state)
    _register_msg(user_id, card_msg_id, sess.id)
    text = _legacy("_render_card")(sess, state, user_id=user_id)
    keyboard = _legacy("build_footer_keyboard")(
        user_id, screen="main", is_busy=_card_is_busy(state)
    )
    if await _legacy("_edit_card")(
        bot, user_id, state, text=text, reply_markup=keyboard
    ):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        return True
    # The message is gone — drop both the cached state and the persisted
    # pointer so the next event creates a fresh card cleanly.
    _cards.pop((user_id, sess.id), None)
    session_manager.clear_card_msg(user_id)
    return False


async def clear_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Wipe the live card's body in response to a user-driven /clear.

    Edits the existing message to a header-only "(cleared)" snapshot
    and keeps an empty, seed-latched state. Dropping the state here would let
    ``resume_card_view`` immediately re-seed the old JSONL transcript and make
    the cleared Telegram history reappear.
    No-op when there is no live card.
    """
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        reset_card(user_id, sess.id)
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.events = []
    state.current_page_idx = None
    state.context_pct = 0
    state.is_continuation = False
    state.in_menu_view = False
    state.kb_prompt = ""
    state.kb_ui_name = ""
    state.in_kb_mode = False
    state.seed_attempted = True
    state.seed_mtime = -1.0
    state.stall_finalized = False
    state.last_rendered = ""
    text = _legacy("_render_card")(sess, state, footer="(cleared)", user_id=user_id)
    cleared_kb = _legacy("build_footer_keyboard")(
        user_id, screen="main", is_busy=False
    )
    await _legacy("_edit_card")(bot, user_id, state, text=text, reply_markup=cleared_kb)
