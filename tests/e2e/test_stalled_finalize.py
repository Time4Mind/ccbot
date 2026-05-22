"""E2E regression (bug A4): stalled-subprocess card finalize.

When the upstream claude process silently stalls, the JSONL stops growing with
renderable entries and the live card freezes on its last non-terminal tail
(thinking / tool_use) forever. ``status_polling.update_status_message`` runs
``maybe_finalize_stalled`` each poll; for an ACTIVE session whose card has a
non-terminal tail, an idle (non-changing) pane, and a stale ``last_event_ts``,
it finalises the card with the stall note via the real ``finalize_task`` path.

This test drives the FULL status-poll entry point (not the unit ``maybe_*``
helper directly) with a FakeTmuxManager returning an idle pane, and asserts the
card msg got edited to carry ``STALL_NOTE``.
"""

from __future__ import annotations

import time

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    STALL_FINALIZE_AFTER_SECONDS,
    STALL_NOTE,
    CardState,
    Event,
)
from ccbot.handlers.status_polling import update_status_message
from ccbot.session import session_manager

from harness import USER_ID, seed_session

WINDOW_ID = "@100"
WORKDIR = "/tmp/proj"
CLAUDE_SID = "44444444-4444-4444-4444-444444444444"

# An idle pane: no spinner status line, no interactive UI. parse_status_line
# returns None here, so pane_busy is False — the stall fingerprint.
IDLE_PANE = "user@host:~/proj$ \n"


def _seed_frozen_card(*, tail_type: str = "tool_use") -> CardState:
    """Install an active-session card whose tail is non-terminal and whose
    last event is older than the stall threshold."""
    now = time.time()
    state = CardState()
    state.msg_id = 7777
    state.last_event_ts = now - (STALL_FINALIZE_AFTER_SECONDS + 30)
    state.last_edit_ts = 0.0
    state.events = [
        Event(type="user_msg", text="do the long thing", started_at=now - 200),
        Event(type=tail_type, text="Bash(make build)", started_at=now - 180),
    ]
    notifications._cards[(USER_ID, "dddd4444")] = state
    return state


@pytest.mark.asyncio
async def test_idle_pane_finalizes_frozen_card(fake_tmux, fake_bot, no_card_lag):
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR, pane=IDLE_PANE)
    seed_session(
        session_manager,
        sid="dddd4444",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )
    _seed_frozen_card(tail_type="tool_use")

    await update_status_message(fake_bot, USER_ID, WINDOW_ID)

    # The card msg was edited to carry the stall note (finalize_task path).
    assert fake_bot.edit_message_text.call_count >= 1
    edit_texts = [e["text"] for e in fake_bot.edits]
    # STALL_NOTE leads with "⚠️ session went idle without a final reply" —
    # match on a distinctive non-escapable fragment.
    assert any("went idle without a final reply" in t for t in edit_texts), edit_texts
    # Card tail is now terminal.
    state = notifications._cards[(USER_ID, "dddd4444")]
    assert state.events[-1].type == "final_text"
    assert STALL_NOTE in state.events[-1].text


@pytest.mark.asyncio
async def test_busy_pane_does_not_finalize(fake_tmux, fake_bot, no_card_lag):
    # A changing spinner = genuine work. The card must NOT be finalized.
    # parse_status_line anchors on the chrome separator below the spinner.
    busy_pane = (
        "Some intermediate output\n"
        "● Working… (18s · ↑1.2k tokens)\n"
        "────────────────────\n"
        "❯\n"
        "────────────────────\n"
        "  ⏵⏵ bypass permissions on\n"
    )
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR, pane=busy_pane)
    seed_session(
        session_manager,
        sid="dddd4444",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )
    _seed_frozen_card(tail_type="thinking")

    await update_status_message(fake_bot, USER_ID, WINDOW_ID)

    state = notifications._cards[(USER_ID, "dddd4444")]
    # Tail stays non-terminal; no stall note appended.
    assert state.events[-1].type == "thinking"
    edit_texts = [e["text"] for e in fake_bot.edits]
    assert not any("went idle without a final reply" in t for t in edit_texts)
