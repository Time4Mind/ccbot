"""A completed history seed must absorb replayed live tool events."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_updates
from ccbot.handlers.card_events import _apply_tool_result, _build_event
from ccbot.handlers.card_pagination import _duplicate_of_seeded
from ccbot.handlers.card_types import CardState, Event
from ccbot.session_monitor import NewMessage


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["Read", "Bash"])
async def test_seeded_completed_tool_absorbs_live_call_and_result(monkeypatch, tool):
    call = NewMessage(
        session_id="native",
        text=f"**{tool}**(input.py)",
        is_complete=True,
        content_type="tool_use",
        tool_use_id="native-call-1",
        tool_name=tool,
        timestamp="2026-10-30T15:00:00+00:00",
    )
    result = NewMessage(
        session_id="native",
        text=f"**{tool}**(input.py)\n  ⎿  Read 3 lines",
        is_complete=True,
        content_type="tool_result",
        tool_use_id="native-call-1",
        tool_name=tool,
        timestamp="2026-10-30T15:00:01+00:00",
    )
    seeded = _build_event(call)
    state = CardState(events=[seeded], seed_attempted=True)
    assert _apply_tool_result(state, _build_event(result))
    monkeypatch.setattr(card_updates, "get_card_state", lambda *_args: state)
    monkeypatch.setattr(card_updates, "_should_buffer", lambda *_args: True)
    monkeypatch.setattr(card_updates, "_legacy", lambda _name: AsyncMock())
    session = SimpleNamespace(id="s1", window_id="")

    await card_updates.update_session_card(SimpleNamespace(), 42, session, call)
    await card_updates.update_session_card(SimpleNamespace(), 42, session, result)

    assert state.events == [seeded]
    assert seeded.completed_at is not None
    assert "Read 3 lines" in seeded.text


def test_distinct_parallel_tool_ids_are_not_deduplicated_by_matching_text_and_time():
    first = Event(type="tool_use", text="Read", started_at=1.0, tool_use_id="call-1")
    second = Event(type="tool_use", text="Read", started_at=1.0, tool_use_id="call-2")
    assert not _duplicate_of_seeded([first], second)


def test_orphan_result_and_late_call_share_tool_identity():
    result = Event(
        type="tool_result", text="Read · done", started_at=2.0, tool_use_id="call-1"
    )
    call = Event(type="tool_use", text="Read", started_at=1.0, tool_use_id="call-1")
    assert _duplicate_of_seeded([result], call)
