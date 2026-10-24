"""Confirm queued final answers only after their exact turn is visibly rendered.

Legacy journals have no separate source-event identity. Match the whole final
and its timestamp, or its exact preceding transcript request, without using a
short answer substring as the identity of a turn.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..final_delivery import FinalDelivery
from .card_types import CardState, Event
from .card_pagination import paginate_events_for_card, _resolved_page_idx


@dataclass
class FinalGroup:
    request: tuple[float, str] | None
    events: list[Event]

    @property
    def text(self) -> str:
        return " ".join(" ".join(event.text for event in self.events).split())


def final_groups(events: list[Event]) -> list[FinalGroup]:
    request = None
    groups: list[FinalGroup] = []
    for index, event in enumerate(events):
        if event.type == "user_msg":
            request = (event.started_at, event.text)
        if event.type != "final_text":
            continue
        previous = events[index - 1] if index else None
        if (
            previous is not None
            and previous.type == "final_text"
            and previous.started_at == event.started_at
        ):
            groups[-1].events.append(event)
        else:
            groups.append(FinalGroup(request, [event]))
    return groups


def visible_final_matches(
    user_id: int,
    state: CardState,
    pending: list[FinalDelivery],
    rendered: str | None = None,
) -> list[FinalDelivery]:
    """Match displayed groups to uniquely identified queued results."""
    from .card_final_delivery import restore_snapshot

    matches_found: list[FinalDelivery] = []
    pages = paginate_events_for_card(state, user_id)
    page = pages[_resolved_page_idx(state, len(pages))]
    visible_ids = {
        id(event)
        for event in page
        if event.type == "final_text"
        and event.text
        and (rendered is None or event.text in rendered)
    }
    visible = [
        group
        for group in final_groups(state.events)
        if any(id(event) in visible_ids for event in group.events)
    ]
    for shown in visible:
        candidates = []
        for item in pending:
            target = restore_snapshot(item)
            final = target.events[item.payload["final_index"]]
            expected = next(
                group
                for group in final_groups(target.events)
                if any(event is final for event in group.events)
            )
            if shown.text != expected.text:
                continue
            if (
                shown.request is not None
                and expected.request is not None
                and shown.request != expected.request
            ):
                continue
            exact_time = (
                shown.events[0].started_at > 0
                and shown.events[0].started_at == expected.events[0].started_at
            )
            exact_request = (
                shown.request is not None
                and shown.request[0] > 0
                and shown.request == expected.request
            )
            if exact_time or exact_request:
                candidates.append((2 if exact_time else 1, item))
        if candidates:
            strongest = max(score for score, _item in candidates)
            matches = [item for score, item in candidates if score == strongest]
            if len(matches) == 1:
                matches_found.append(matches[0])
    return matches_found


def acknowledge_visible_final(
    user_id: int, session_id: str, state: CardState, rendered: str
) -> None:
    """Caller has confirmed this exact card payload through Telegram."""
    from ..final_delivery import final_delivery_queue

    pending = final_delivery_queue.pending(user_id, session_id)
    if not pending or state.delivery_id or state.in_menu_view or state.in_kb_mode:
        return
    for item in visible_final_matches(user_id, state, pending, rendered):
        if item in final_delivery_queue.records:
            if state.msg_id is not None:
                final_delivery_queue.bind_ack(item.id, state.msg_id)
            final_delivery_queue.resolve(item)


def observe_card_receipt(user_id: int, state: CardState, rendered: str) -> None:
    """Restrict transport receipts to the currently selected card owner."""
    if state.msg_id is None or state.delivery_id or state.in_menu_view:
        return
    from ..session import session_manager
    from .card_registry import lookup_session_for_message

    session_id = lookup_session_for_message(user_id, state.msg_id)
    active = session_manager.get_active_session(user_id)
    if active is not None and active.id == session_id:
        acknowledge_visible_final(user_id, active.id, state, rendered)
