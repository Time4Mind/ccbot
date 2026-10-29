"""Bridge durable final deliveries to the existing rich card transport.

Each obligation owns a frozen turn snapshot. A later prompt, pagination tap or
session switch cannot change the answer waiting to be delivered.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta
import logging
from typing import Any

from telegram import Bot
from telegram.error import RetryAfter

from ..final_delivery import FinalDelivery
from ..session import Session, session_manager
from ..telegram_rate_limit import background_telegram_request
from .card_binding import restore_carrier, snapshot_carrier
from .card_types import CardState, Event, TurnPhase
from .card_registry import _card_lock, _carrier_edit_lock, lookup_session_for_message

logger = logging.getLogger(__name__)


def final_payload(
    state: CardState, final_event: Event, old_message_id: int | None
) -> dict[str, Any]:
    events = []
    for event in state.events:
        # Tool-result images are not part of final text rendering and need not
        # duplicate binary transcript payloads in the delivery journal.
        row = asdict(event)
        row["image_data"] = None
        events.append(row)
    final_index = next(
        index for index, event in enumerate(state.events) if event is final_event
    )
    return {
        "final_index": final_index,
        "text": None,
        "old_message_id": old_message_id,
        "state": {
            "events": events,
            "current_page_idx": state.current_page_idx,
            "next_request_sequence": state.next_request_sequence,
            "last_event_ts": state.last_event_ts,
            "agent_model": state.agent_model,
            "reasoning_effort": state.reasoning_effort,
            "context_pct": state.context_pct,
            "completion_marker_pending": True,
            "media_anchor_offset": state.media_anchor_offset,
        },
    }


def restore_snapshot(item: FinalDelivery) -> CardState:
    raw = dict(item.payload["state"])
    raw["events"] = [Event(**row) for row in raw["events"]]
    return CardState(
        **raw, turn_phase=TurnPhase.IDLE, delivery_id=item.id, msg_id=item.message_id
    )


async def deliver_final(
    bot: Bot,
    item: FinalDelivery,
    *,
    sess: Session | None = None,
    live: CardState | None = None,
) -> bool | None:
    """Attempt under the session lock; caller owns that lock on initial send."""
    from ..final_delivery import final_delivery_queue
    from . import bg_status
    from .card_updates import (
        _should_buffer,
        _render_with_final_proof,
        _legacy,
        get_card_state,
    )

    if item not in final_delivery_queue.records:
        return True
    if item.message_id is not None:
        # The transport checkpoints this receipt before stripping keyboards.
        # A bookkeeping crash must not manufacture another final card.
        return True
    sess = sess or session_manager.get_session(item.session_id)
    if sess is None:
        return None
    live = live or get_card_state(item.user_id, sess)
    if _should_buffer(item.user_id, item.session_id, live):
        return None
    from .card_delivery_receipts import visible_final_matches

    passive = live.turn_phase is not TurnPhase.IDLE or bool(
        live.events and item not in visible_final_matches(item.user_id, live, [item])
    )
    preserve_page = passive and (
        live.turn_phase is TurnPhase.IDLE or live.current_page_idx is not None
    )
    target = restore_snapshot(item)
    # The older answer becomes the newest visible carrier while the live
    # state continues tracking the newer request and its running phase.
    if passive:
        target.next_request_sequence = live.next_request_sequence
    logger.info(
        "final_delivery_route id=%s session=%s live_message=%s passive=%s sequence=%s",
        item.id,
        item.session_id,
        live.msg_id,
        passive,
        target.next_request_sequence,
    )
    text = item.payload["text"]
    if text is None:
        final = target.events[item.payload["final_index"]]
        text = _render_with_final_proof(sess, target, item.user_id, final)
        if text is None:
            return False
        item.payload["text"] = text
        item.payload["state"]["media_anchor_offset"] = target.media_anchor_offset
        final_delivery_queue.save()
    keyboard = _legacy("build_footer_keyboard")(
        item.user_id, screen="main", is_busy=live.turn_phase is not TurnPhase.IDLE
    )
    async with _carrier_edit_lock(item.user_id):
        if item not in final_delivery_queue.records:
            return True
        if _should_buffer(item.user_id, item.session_id, live):
            return None
        if preserve_page and target.msg_id is None:
            # A manually selected page remains in place during an old retry.
            from .message_sender import send_with_fallback

            with background_telegram_request():
                sent_message = await send_with_fallback(
                    bot, item.user_id, text, disable_notification=True
                )
            if sent_message is None:
                return False
            final_delivery_queue.bind_ack(item.id, sent_message.message_id)
            logger.info(
                "final_delivery_passive id=%s session=%s message_id=%s live_message=%s",
                item.id,
                item.session_id,
                sent_message.message_id,
                live.msg_id,
            )
            return True
        try:
            with background_telegram_request():
                sent = await _legacy("_send_card")(
                    bot, item.user_id, sess, target, text=text, reply_markup=keyboard
                )
        except RetryAfter as send_limit:
            # A rejected new-message operation does not imply that an edit of
            # the existing carrier is also blocked. Repaint only the exact
            # current turn; never overwrite a newer turn or another session.
            active = session_manager.get_active_session(item.user_id)
            if (
                passive
                or live.turn_phase is not TurnPhase.IDLE
                or live.next_request_sequence != target.next_request_sequence
                or item.message_id is not None
                or live.msg_id is None
                or live.msg_id != item.payload.get("old_message_id")
                or lookup_session_for_message(item.user_id, live.msg_id)
                != item.session_id
                or active is None
                or active.id != item.session_id
                or item not in visible_final_matches(item.user_id, live, [item], text)
            ):
                raise
            from .card_transport import _edit_card_unlocked

            try:
                with background_telegram_request():
                    edited = await _edit_card_unlocked(
                        bot,
                        item.user_id,
                        live,
                        text=text,
                        reply_markup=keyboard,
                        refresh_pane=False,
                    )
            except RetryAfter as edit_limit:

                def seconds(error: RetryAfter) -> float:
                    wait = error.retry_after
                    return (
                        wait.total_seconds()
                        if isinstance(wait, timedelta)
                        else float(wait)
                    )

                raise (
                    send_limit
                    if seconds(send_limit) >= seconds(edit_limit)
                    else edit_limit
                )
            if not edited:
                raise
            live.last_rendered = text
            if item in final_delivery_queue.records:
                final_delivery_queue.bind_ack(item.id, live.msg_id)
            remaining = final_delivery_queue.pending(item.user_id, item.session_id)
            if not remaining or remaining == [item]:
                bg_status.record_finished_delivery(item.user_id, item.session_id)
            logger.info(
                "final_delivery_edited id=%s session=%s message_id=%s",
                item.id,
                item.session_id,
                live.msg_id,
            )
            return True
        delivered = sent is not False and target.msg_id is not None
    if not delivered:
        return False
    if target.msg_id is not None:
        final_delivery_queue.bind_ack(item.id, target.msg_id)
    if (
        _should_buffer(item.user_id, item.session_id, live)
        or live.next_request_sequence != target.next_request_sequence
    ):
        return True
    old_id = item.payload.get("old_message_id")
    if old_id and old_id != target.msg_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=item.user_id, message_id=old_id, reply_markup=None
            )
        except Exception as exc:
            logger.debug("final card keyboard strip failed: %s", exc)
    restore_carrier(live, snapshot_carrier(target))
    live.last_rendered = text
    # Only the last queued final may acknowledge the session's latest result.
    if (
        not passive
        and len(final_delivery_queue.pending(item.user_id, item.session_id)) == 1
    ):
        bg_status.record_finished_delivery(item.user_id, item.session_id)
    return True


async def retry_delivery(bot: Bot, item: FinalDelivery) -> bool | None:
    async with _card_lock(item.user_id, item.session_id):
        return await deliver_final(bot, item)


def start_final_deliveries(bot: Bot) -> None:
    from ..final_delivery import final_delivery_queue

    def ready(item: FinalDelivery) -> bool:
        from .card_registry import _cards, _should_buffer

        sess = session_manager.get_session(item.session_id)
        return sess is not None and not _should_buffer(
            item.user_id,
            item.session_id,
            _cards.get((item.user_id, item.session_id), CardState()),
        )

    final_delivery_queue.start(lambda item: retry_delivery(bot, item), ready=ready)
