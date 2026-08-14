"""Completed live-card pages are not reformatted on every stream event."""

from __future__ import annotations

import time

from ccbot.handlers import card_pagination
from ccbot.handlers.card_types import CardState, Event


def _event(text: str, *, page_break: bool = False) -> Event:
    return Event(
        type="final_text" if page_break else "tool_use",
        text=text,
        body=text,
        started_at=time.time(),
        completed_at=time.time(),
        is_page_break=page_break,
    )


def test_stream_append_reuses_completed_prefix(monkeypatch) -> None:
    state = CardState(
        events=[
            _event("answer one", page_break=True),
            _event("old tool"),
            _event("answer two", page_break=True),
            _event("current tool"),
        ]
    )
    real_split = card_pagination._split_page_by_budget
    calls: list[list[Event]] = []

    def recording_split(page: list[Event], budget: int):
        calls.append(page)
        return real_split(page, budget)

    monkeypatch.setattr(card_pagination, "_split_page_by_budget", recording_split)
    first = card_pagination.paginate_events_for_card(state, None)
    assert len(calls) == 2  # completed page + current page

    calls.clear()
    state.events.append(_event("next streaming tool"))
    second = card_pagination.paginate_events_for_card(state, None)

    assert len(calls) == 1
    assert calls[0] == state.events[2:]
    assert second[0] is first[0]


def test_new_answer_rebuilds_prefix_once(monkeypatch) -> None:
    state = CardState(
        events=[
            _event("answer one", page_break=True),
            _event("old tool"),
            _event("answer two", page_break=True),
        ]
    )
    card_pagination.paginate_events_for_card(state, None)
    real_split = card_pagination._split_page_by_budget
    calls = 0

    def recording_split(page: list[Event], budget: int):
        nonlocal calls
        calls += 1
        return real_split(page, budget)

    monkeypatch.setattr(card_pagination, "_split_page_by_budget", recording_split)
    state.events.append(_event("answer three", page_break=True))
    card_pagination.paginate_events_for_card(state, None)
    first_rebuild_calls = calls
    assert first_rebuild_calls >= 2

    calls = 0
    state.events.append(_event("streaming after answer"))
    card_pagination.paginate_events_for_card(state, None)
    assert calls == 1
