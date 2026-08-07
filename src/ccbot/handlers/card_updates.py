"""Apply session events, finalize tasks, and deliver card attachments."""

from __future__ import annotations

from __future__ import annotations

import asyncio
import logging
import time

from telegram import Bot

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from .card_model import (
    CARD_MAX_EVENTS,
    CardState,
    Event,
    _apply_tool_result,
    _build_event,
    _chunk_final_text,
    _duplicate_of_seeded,
    _is_stale,
    _resolve_line_budget,
    _strip_for_card,
    paginate_events_for_card,
)
from .switcher import session_emoji
from .tg_format import Attachment, split_overflow

from .card_registry import (
    _card_lock,
    _register_msg,
    _should_buffer,
    reset_card,
    _legacy,
)
from .card_seed import get_card_state
from .card_carrier import _recover_from_false_stall
from .card_transport import _deferred_edit

logger = logging.getLogger(__name__)


__all__ = [
    "update_session_card",
    "_update_session_card_locked",
    "finalize_task",
    "_send_attachments",
    "push_event",
]


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Append `msg` to the session's live card, or open a new one if needed.

    Triggers a fresh card on long pause and on hard-limit overflow.
    """
    # Fire a (throttled) background prewarm of the pages cache so the
    # live-card's ◀ Older N/N counter has a value to render on the
    # next event. The first event after session start may still paint
    # without the counter — the background task lands within a second.
    if sess.window_id:
        from .history import kick_prewarm

        kick_prewarm(sess.window_id)

    state = get_card_state(user_id, sess)
    # First event after a bot restart: pull JSONL history into events
    # so the card shows context, not a single 1/1 page.
    await _legacy("_ensure_seeded")(user_id, sess, state)

    # Should we buffer this event instead of rendering it now? Reasons:
    # - the user is on a Menu / sub-screen (state.in_menu_view set by
    #   pause_card_view or transfer_card_to_carrier);
    # - the session isn't the user's currently-active one (live check —
    #   bg sessions must stay silent in chat).
    # The check is in ``_should_buffer`` so future buffering reasons
    # converge on the same predicate. Previously the bg branch was
    # implemented by force-setting ``state.in_menu_view = True`` here;
    # the flag was sticky and outlived the bg phase, leaving the card
    # permanently paused — until a typed message woke
    # ``resume_card_view``. Computing the bg check live fixes that.
    must_buffer = _should_buffer(user_id, sess.id, state)

    msg_id_in = state.msg_id
    in_menu_view_in = state.in_menu_view

    new_event = _build_event(msg)
    # tool_result: fold into the matching tool_use Event in place.
    # If no match (race / restart), append the placeholder as a row.
    replaced = False
    if msg.content_type == "tool_result":
        replaced = _apply_tool_result(state, new_event)

    # Buffer-only path: user is on a menu/sub-screen OR session is bg.
    # Buffer the event into ``state.events`` so resume / next switcher
    # tap can catch up; do NOT trigger stale-card resets, overflow
    # continuations, or any rendering.
    if must_buffer:
        if not replaced and not _duplicate_of_seeded(state.events, new_event):
            # Same dedup guard as the live path: a prior seed (line ~1371)
            # may already hold this turn; don't buffer a second copy.
            state.events.append(new_event)
        state.last_event_ts = time.time()
        logger.info(
            "card_update buffered sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            msg_id_in,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_buffered",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": msg_id_in,
                "content_type": msg.content_type,
                "lines": len(state.events),
                "in_menu_view": in_menu_view_in,
            },
        )
        return

    # Spawn-serialization (Task #50): hold the per-session lock from
    # the stale-check through the actual send/edit. Otherwise two
    # concurrent ``update_session_card`` calls (or one
    # ``update_session_card`` racing with ``resume_card_view`` /
    # ``repost_card`` / ``finalize_task``) can both see ``msg_id is
    # None`` and both spawn — produces "2 messages in wrong order".
    async with _card_lock(user_id, sess.id):
        return await _update_session_card_locked(
            bot, user_id, sess, msg, state, new_event, replaced
        )


async def _update_session_card_locked(
    bot: Bot,
    user_id: int,
    sess: Session,
    msg: NewMessage,
    state: CardState,
    new_event: Event,
    replaced: bool,
) -> None:
    # Recover from a prior false-positive stall_finalize. Wipe the card
    # binding so this real assistant turn lands on a fresh message
    # below the stalled stub instead of being silently edited into it.
    if state.stall_finalized:
        _recover_from_false_stall(state)
        await _legacy("_ensure_seeded")(user_id, sess, state)
    # Trigger: long pause → fresh card.
    if _is_stale(state):
        state.msg_id = None
        state.events = []
        state.current_page_idx = None
        state.is_continuation = True
        state.last_rendered = ""
        # A6: re-seed the recent transcript so the fresh card lands with
        # its full turn-history. Without this the card rebuilds one event
        # at a time and the footer page counter shows ``1/1`` until a
        # second turn completes — even though the transcript is long.
        state.seed_attempted = False
        state.seed_mtime = -1.0
        await _legacy("_ensure_seeded")(user_id, sess, state)

    if not replaced and not _duplicate_of_seeded(state.events, new_event):
        # Dedup guard: if the stale-branch re-seed above (or an earlier
        # release_card_message wipe) already pulled this turn in from
        # JSONL, don't append it a second time — otherwise the user's own
        # message renders twice in the card body.
        state.events.append(new_event)
        # User-action-anchor: when on the latest page, every new event
        # keeps the user there. Done as None (=stick-to-latest) so the
        # render layer picks the latest page automatically.
        # Page idx is recalibrated by paginate-aware callbacks.

    # Cap event log to avoid unbounded memory; FIFO evicts oldest.
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]

    state.last_event_ts = time.time()

    # Pagination handles size: the latest page is always within
    # CARD_HARD_LIMIT chars (paginate splits before the boundary).
    # No continuation-card path.

    text = _legacy("_render_card")(sess, state, user_id=user_id)

    if state.msg_id is None:
        await _legacy("_send_card")(bot, user_id, sess, state, text=text)
        state.last_edit_ts = time.monotonic()
        logger.info(
            "card_update sent sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            state.msg_id,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_sent",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": state.msg_id,
                "content_type": msg.content_type,
                "lines": len(state.events),
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
        edited = await _legacy("_edit_card")(bot, user_id, state, text=text)
        if edited:
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            logger.info(
                "card_update edit sess=%s msg_id=%s ctype=%s lines=%d",
                sess.id,
                state.msg_id,
                msg.content_type,
                len(state.events),
                extra={
                    "event": "card_update_edited",
                    "user_id": user_id,
                    "session_id": sess.id,
                    "msg_id": state.msg_id,
                    "content_type": msg.content_type,
                    "lines": len(state.events),
                },
            )
        else:
            # Edit failed AND we couldn't recover — DO NOT fall back to
            # _send_card here. Sending a new message produces duplicate
            # cards in chat (this was the "2 messages in a row" bug:
            # Message_too_long → fallback send → stale card stays + new
            # appears). Caller's next event retries the edit; if the
            # carrier message is truly gone (deleted, too old), the
            # ``Message to edit not found`` branch in _edit_card resets
            # msg_id and a fresh card spawns on the next event.
            state.last_edit_ts = time.monotonic()
            logger.warning(
                "card_update edit_failed sess=%s msg_id=%s — keeping "
                "stale card; new render will retry on next event",
                sess.id,
                state.msg_id,
            )
        return

    # Inside the coalescing window: ensure exactly one deferred edit is queued.
    if state.pending_edit is None or state.pending_edit.done():
        delay = max(0.05, lag - elapsed)
        state.pending_edit = asyncio.create_task(
            _deferred_edit(bot, user_id, sess, state, delay)
        )


async def finalize_task(bot: Bot, user_id: int, sess: Session, final_text: str) -> None:
    """Append the final assistant answer to the current live card.

    Appends a ``final_text`` Event with ``is_page_break=True`` so the
    new answer anchors the top of the latest page; everything before
    it (tool log, thinking, mid-stream text) lives on the previous page.
    The user lands on the new latest page by default. Long answers
    that exceed Telegram's 4096-char limit are sub-paginated by
    ``paginate_events``.
    """
    state = get_card_state(user_id, sess)
    # Recover from a prior false-positive stall_finalize: wipe the card
    # binding so this real answer spawns a fresh card below the stalled
    # stub. Must run before ``_ensure_seeded`` so the seed targets the
    # cleared events list. NOT triggered by ``maybe_finalize_stalled``'s
    # own call into ``finalize_task`` — the flag is set only AFTER that
    # path returns.
    if state.stall_finalized:
        _recover_from_false_stall(state)
    # First event after a bot restart: seed JSONL history before
    # appending the final answer so the user sees their context.
    await _legacy("_ensure_seeded")(user_id, sess, state)

    # Bg-session silence + menu-pause buffering. Same predicate as
    # update_session_card — see ``_should_buffer`` for the rationale.
    must_buffer = _should_buffer(user_id, sess.id, state)

    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None

    cleaned = (final_text or "").strip()
    if not cleaned:
        # No final text from Claude (e.g. /clear with nothing else).
        # Drop the card; no push — the previous "completion push"
        # behaviour was removed when the result moved into the card body.
        if must_buffer:
            return
        reset_card(user_id, sess.id)
        return

    formatted = split_overflow(cleaned)
    cleaned = formatted.text
    attachments = formatted.attachments

    # Final answer = ONE OR MORE is_page_break Events: each chunk
    # anchors a new page. ``_chunk_final_text`` keeps every chunk under
    # ``card_page_lines`` user-setting (in LINES) so the rendered page
    # respects what the user picked. Smart boundaries (paragraph / line
    # / sentence / word) prevent mid-content breaks. Default focus lands
    # on the FIRST chunk's page so the user reads the answer from the top.
    now = time.time()
    stripped_full = _strip_for_card(cleaned)
    chunks = _chunk_final_text(stripped_full, _resolve_line_budget(user_id))
    final_events = [
        Event(
            type="final_text",
            text=chunk,
            body=chunk,
            started_at=now,
            completed_at=now,
            is_page_break=True,
        )
        for chunk in chunks
    ]

    # Buffer-only path: user is on a Menu view OR session is bg.
    # Accumulate the answer Events into state. resume_card_view (next
    # typed message) / switcher tap will render the catch-up.
    # Attachments still go out (file delivery shouldn't wait on UI nav).
    if must_buffer:
        state.events.extend(final_events)
        state.last_event_ts = now
        if attachments:
            await _legacy("_send_attachments")(bot, user_id, attachments)
        return

    state.events.extend(final_events)
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]
    state.last_event_ts = now
    # Default focus: when the answer was split into N chunks, land on
    # the FIRST chunk's page so the user starts at the top. When there's
    # only one chunk, ``None`` = latest, which is that same page.
    if len(final_events) > 1:
        pages_after = paginate_events_for_card(state, user_id)
        # The first chunk's page is at index (len(pages) - len(chunks)).
        first_chunk_page = max(0, len(pages_after) - len(final_events))
        state.current_page_idx = first_chunk_page
    else:
        state.current_page_idx = None

    # Refresh the pages cache so the live-card's pagination counter
    # reflects the final transcript length on the finalised message.
    # This is the one place we await prewarm directly — finalize fires
    # once per task, so ~1 s of parsing is OK to pay for a correct
    # ◀ Older N/N counter on the artifact the user looks at most.
    if sess.window_id:
        try:
            from .history import prewarm_pages_cache

            await prewarm_pages_cache(sess.window_id)
        except Exception as e:
            logger.debug("finalize_task prewarm failed: %s", e)

    # Final answer → ``is_busy=False`` keyboard so the user sees Kill,
    # not Stop. State stays live (rolling card); the next turn's events
    # keep editing the SAME message — no reset_card, no pin.
    done_kb = _legacy("build_footer_keyboard")(
        user_id, screen="main", is_busy=False
    )

    text = _legacy("_render_card")(sess, state, user_id=user_id)
    # Lock the spawn/edit decision so a parallel ``update_session_card``
    # for the next turn can't see ``msg_id is None`` simultaneously and
    # spawn a second card (Task #50).
    async with _card_lock(user_id, sess.id):
        if state.msg_id is None:
            await _legacy("_send_card")(
                bot, user_id, sess, state, text=text, reply_markup=done_kb
            )
        elif await _legacy("_edit_card")(
            bot, user_id, state, text=text, reply_markup=done_kb
        ):
            state.last_rendered = text
        state.last_edit_ts = time.monotonic()

    if attachments:
        await _legacy("_send_attachments")(bot, user_id, attachments)


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
    """Bg-session push — a bare one-line notification.

    Format is strictly ``<emoji> <name> <text>``: no markdown brackets,
    no inline keyboard, no switcher migration. Hijacking the active
    card's footer buttons (the previous behaviour) confused users —
    bg pushes are status pings, not navigation surfaces. Use the
    switcher on the active card to actually visit the session.
    """
    emoji = "🟥" if is_error else session_emoji(sess)
    name = sess.name or sess.id
    body = f"{emoji} {name} {text}"
    if len(body) > 3500:
        body = body[:3497] + "…"
    try:
        sent = await _legacy("safe_send")(bot, user_id, body)
    except Exception as e:
        logger.debug("push_event failed: %s", e)
        return
    # Register the msg→session map so a reply-quote to this push still
    # routes back to the originating session.
    if sent is not None:
        _register_msg(user_id, sent.message_id, sess.id)
