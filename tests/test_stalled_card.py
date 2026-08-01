"""Regression tests for bug A4 — stalled-session card rescue.

When the upstream claude subprocess silently stalls or exits mid-turn,
the JSONL stops growing with renderable entries (it may still get
``last-prompt`` / ``ai-title`` metadata, which transcript_parser filters
out), so the session monitor produces ZERO card updates and the live
card freezes on its last "thinking" / tool_use frame forever, with no
signal to the user.

``notifications.maybe_finalize_stalled`` closes that gap: for an ACTIVE
session whose card has a non-terminal tail event and an idle (non-busy)
pane that has stayed that way for ``STALL_FINALIZE_AFTER_SECONDS``, it
finalises the card with a clear note via the normal ``finalize_task``
path. These tests pin the trigger condition and the negative cases that
must NOT fire (still-changing spinner, already-finalized card, waiting
interactive UI / kb prompt, menu navigation, too-recent event).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    STALL_FINALIZE_AFTER_SECONDS,
    STALL_FINALIZE_TOOL_USE_SECONDS,
    STALL_NOTE,
    CardState,
    Event,
    _cards,
    maybe_finalize_stalled,
)
from ccbot.session_models import Session


@pytest.fixture(autouse=True)
def _clear_cards():
    _cards.clear()
    yield
    _cards.clear()


@pytest.fixture
def stub_finalize(monkeypatch):
    """Replace ``finalize_task`` with a recorder so we only assert the
    trigger decision, not the full card-render machinery."""
    calls: list[tuple[int, str, str]] = []

    async def _fake_finalize(bot, user_id, sess, final_text):  # type: ignore[no-untyped-def]
        calls.append((user_id, sess.id, final_text))

    monkeypatch.setattr(notifications, "finalize_task", _fake_finalize)
    return calls


def _make_sess(sid: str = "s1") -> Session:
    return Session(
        id=sid,
        name="tests",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="uuid-" + sid,
    )


def _seed_card(
    user_id: int,
    sess: Session,
    *,
    tail_type: str = "thinking",
    msg_id: int | None = 100,
    age_seconds: float = STALL_FINALIZE_AFTER_SECONDS + 30,
    in_menu_view: bool = False,
    in_kb_mode: bool = False,
) -> CardState:
    """Install a CardState whose last event is ``tail_type`` and which
    last updated ``age_seconds`` ago."""
    now = time.time()
    state = CardState()
    state.msg_id = msg_id
    state.in_menu_view = in_menu_view
    state.in_kb_mode = in_kb_mode
    state.last_event_ts = now - age_seconds
    state.events = [
        Event(type="user_msg", text="do the thing", started_at=now - age_seconds - 5),
        Event(type=tail_type, text="", started_at=now - age_seconds),
    ]
    _cards[(user_id, sess.id)] = state
    return state


# ── Positive: the trigger fires ───────────────────────────────────────


class TestStallFires:
    @pytest.mark.asyncio
    async def test_idle_nonterminal_tail_finalizes(self, stub_finalize):
        """Card non-finalized + pane idle + last event stale + no UI/menu
        => stalled-finalize path invoked with the stall note."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="thinking")

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is True
        assert stub_finalize == [(user_id, sess.id, STALL_NOTE)]

    @pytest.mark.asyncio
    async def test_tool_use_tail_finalizes(self, stub_finalize):
        """A tool_use whose result never came is also a stall fingerprint
        once the pane has been idle long enough — but the threshold is
        the longer ``STALL_FINALIZE_TOOL_USE_SECONDS`` because slow tools
        and post-tool reasoning are legitimately silent for minutes."""
        user_id, sess = 42, _make_sess()
        _seed_card(
            user_id,
            sess,
            tail_type="tool_use",
            age_seconds=STALL_FINALIZE_TOOL_USE_SECONDS + 30,
        )

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is True
        assert len(stub_finalize) == 1

    @pytest.mark.asyncio
    async def test_tool_use_within_extended_threshold_no_fire(self, stub_finalize):
        """A tool_use tail idle for ``STALL_FINALIZE_AFTER_SECONDS`` but
        under ``STALL_FINALIZE_TOOL_USE_SECONDS`` must NOT finalize — this
        is the metrics-debug regression: Claude was reasoning ~96 s after
        the last tool_use before emitting the final answer, which the
        old single-threshold policy treated as a stall."""
        user_id, sess = 42, _make_sess()
        _seed_card(
            user_id,
            sess,
            tail_type="tool_use",
            age_seconds=STALL_FINALIZE_AFTER_SECONDS + 30,
        )

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_stall_arms_recovery_flag(self, monkeypatch):
        """After ``finalize_task`` lands the STALL_NOTE, the card state
        must carry ``stall_finalized=True`` so the next genuine assistant
        turn spawns a fresh card instead of silently editing the stub."""

        # Real ``finalize_task`` replacement that mirrors the actual
        # contract: append a ``final_text`` Event so the post-call check
        # sees a terminal tail (defensive — recovery flag is set after
        # finalize returns, irrespective of internals).
        async def _fake_finalize(bot, user_id, sess, final_text):  # type: ignore[no-untyped-def]
            st = _cards.get((user_id, sess.id))
            if st is not None:
                st.events.append(
                    Event(type="final_text", text=final_text, started_at=time.time())
                )

        monkeypatch.setattr(notifications, "finalize_task", _fake_finalize)

        user_id, sess = 42, _make_sess()
        state = _seed_card(
            user_id,
            sess,
            tail_type="tool_use",
            age_seconds=STALL_FINALIZE_TOOL_USE_SECONDS + 30,
        )
        assert state.stall_finalized is False

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is True
        assert _cards[(user_id, sess.id)].stall_finalized is True


# ── Negative: the trigger must stay quiet ─────────────────────────────


class TestStallSuppressed:
    @pytest.mark.asyncio
    async def test_spinner_still_changing_no_fire(self, stub_finalize):
        """A still-changing spinner (``pane_busy=True``) is genuine work,
        not a stall — never finalize."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="thinking")

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=True,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_already_finalized_no_fire(self, stub_finalize):
        """A card whose tail is ``final_text`` is already done — nothing
        frozen to rescue."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="final_text")

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_error_tail_no_fire(self, stub_finalize):
        """An ``error`` tail is also terminal — already finalized."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="error")

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_interactive_ui_waiting_no_fire(self, stub_finalize):
        """An AskUserQuestion / ExitPlanMode / permission prompt waiting
        for the user is a valid idle state, not a stall."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="thinking")

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=True,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_kb_mode_no_fire(self, stub_finalize):
        """Card in kb-mode (prompt rendered on the card) is awaiting the
        user — never a stall."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="thinking", in_kb_mode=True)

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_menu_view_no_fire(self, stub_finalize):
        """User browsing a Menu sub-screen on the carrier — suppress."""
        user_id, sess = 42, _make_sess()
        _seed_card(user_id, sess, tail_type="thinking", in_menu_view=True)

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=True,  # status_polling passes in_menu too
            in_menu=True,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_recent_event_no_fire(self, stub_finalize):
        """Idle window not yet elapsed (last event within the threshold) —
        an ordinary intra-turn gap, not a stall."""
        user_id, sess = 42, _make_sess()
        _seed_card(
            user_id,
            sess,
            tail_type="thinking",
            age_seconds=STALL_FINALIZE_AFTER_SECONDS - 10,
        )

        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )

        assert fired is False
        assert stub_finalize == []

    @pytest.mark.asyncio
    async def test_no_card_no_fire(self, stub_finalize):
        """No card / no msg_id => nothing to finalize."""
        user_id, sess = 42, _make_sess()
        # No card seeded at all.
        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )
        assert fired is False

        # Card present but msg_id is None (e.g. after Shot closed it).
        _seed_card(user_id, sess, tail_type="thinking", msg_id=None)
        fired = await maybe_finalize_stalled(
            AsyncMock(),
            user_id,
            sess,
            pane_busy=False,
            interactive_waiting=False,
            in_menu=False,
        )
        assert fired is False
        assert stub_finalize == []
