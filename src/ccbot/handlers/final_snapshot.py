"""Compact, backwards-readable snapshots for durable final-answer delivery."""

from dataclasses import asdict
from typing import Any

from .card_pagination import _resolved_page_idx, paginate_events_for_card
from .card_types import CardState, Event


def _answer_events(
    state: CardState, index: int, user_id: int | None
) -> tuple[list[Event], int, int | None]:
    """Keep the answer's logical pages plus the request used for receipt matching."""
    events = state.events
    first, end = index, index + 1
    while first and events[first - 1].type == "final_text":
        first -= 1
    while end < len(events) and events[end].type == "final_text":
        end += 1
    # Non-breaking annotations following an answer share its last page.
    while (
        end < len(events)
        and not events[end].is_page_break
        and events[end].type != "final_text"
    ):
        end += 1
    request = next(
        (events[i] for i in range(first - 1, -1, -1) if events[i].type == "user_msg"),
        None,
    )
    prefix = [request] if request is not None else []
    kept = prefix + events[first:end]
    offset = len(prefix) + index - first
    selected = state.current_page_idx
    if len(kept) != len(events):
        selected = len(prefix)
        # A saved multi-page answer must still open on its original visible chunk.
        if sum(event.type == "final_text" for event in kept) > 1:
            pages = paginate_events_for_card(state, user_id)
            visible = {
                id(event)
                for event in pages[_resolved_page_idx(state, len(pages))]
                if event.type == "final_text"
            }
            compact_pages = paginate_events_for_card(CardState(events=kept), user_id)
            selected = next(
                (
                    i
                    for i, page in enumerate(compact_pages)
                    if any(id(event) in visible for event in page)
                ),
                selected,
            )
    return kept, offset, selected


def compact_legacy_snapshot(payload: dict[str, Any], user_id: int) -> bool:
    state = payload.get("state")
    if not isinstance(state, dict) or "final_index" not in payload:
        return False
    rows = state["events"]
    index = payload["final_index"]
    original = CardState(
        events=[Event(**row) for row in rows],
        current_page_idx=state.get("current_page_idx"),
    )
    events, offset, selected = _answer_events(original, index, user_id)
    if len(events) == len(rows):
        return False
    state["events"] = [asdict(event) | {"image_data": None} for event in events]
    state["current_page_idx"] = selected
    payload["final_index"] = offset
    return True


def final_payload(
    state: CardState,
    final_event: Event,
    old_message_id: int | None,
    user_id: int | None = None,
) -> dict[str, Any]:
    final_index = next(
        index for index, event in enumerate(state.events) if event is final_event
    )
    events, final_index, selected = _answer_events(state, final_index, user_id)
    return {
        "final_index": final_index,
        "text": None,
        "old_message_id": old_message_id,
        "state": {
            "events": [asdict(event) | {"image_data": None} for event in events],
            "current_page_idx": selected,
            "next_request_sequence": state.next_request_sequence,
            "last_event_ts": state.last_event_ts,
            "agent_model": state.agent_model,
            "reasoning_effort": state.reasoning_effort,
            "context_pct": state.context_pct,
            "completion_marker_pending": True,
            "media_anchor_offset": state.media_anchor_offset,
        },
    }
