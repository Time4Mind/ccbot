"""``_poll_usage_modal`` must only publish a SETTLED /usage read.

An unsettled frame — a transitional value mid-load, or a stale pre-reset
render still in scrollback — used to be returned via a fallback, and the
quota-alerts loop turned it into a phantom threshold crossing (observed:
``week: 78%`` pushed while the live week was 2%). The poller now returns
``None`` unless two consecutive captures agree.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator

import pytest

from ccbot.bot import _usage_window
from ccbot.terminal_parser import extract_usage_breakdown


def _frame(session: int, week: int, sonnet: int) -> str:
    """A minimal /usage modal body the real parser understands."""
    return (
        f"Current session\n█  {session}% used\nResets 4pm (UTC)\n\n"
        f"Current week (all models)\n█  {week}% used\nResets Jun 21 at 1pm (UTC)\n\n"
        f"Current week (Sonnet)\n█  {sonnet}% used\nResets Jun 21 at 1pm (UTC)\n"
    )


@pytest.fixture(autouse=True)
def _fast_and_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """No real tmux, no real 200 ms sleeps."""

    async def _noop_sleep(*_a: object, **_k: object) -> None:
        return None

    async def _noop_send(*_a: object, **_k: object) -> bool:
        return True

    async def _noop_clear(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(_usage_window.asyncio, "sleep", _noop_sleep)
    monkeypatch.setattr(_usage_window.tmux_manager, "send_keys", _noop_send)
    monkeypatch.setattr(_usage_window, "_clear_pane_history", _noop_clear)


def _capture_returning(frames: Iterator[str]):
    async def _cap(_wid: str) -> str:
        return next(frames)

    return _cap


@pytest.mark.asyncio
async def test_settled_read_is_published(monkeypatch: pytest.MonkeyPatch):
    # Two consecutive identical captures → settled → value returned.
    frames = iter([_frame(7, 2, 0), _frame(7, 2, 0)])
    monkeypatch.setattr(
        _usage_window, "_capture_with_scrollback", _capture_returning(frames)
    )

    info = await _usage_window._poll_usage_modal("@1")

    assert info is not None
    assert extract_usage_breakdown(info).week_pct == 2


@pytest.mark.asyncio
async def test_unsettled_read_returns_none(monkeypatch: pytest.MonkeyPatch):
    # Week oscillates forever; no two consecutive captures ever agree, so
    # the modal never settles. The OLD code returned the last (78%) frame;
    # the fix returns None so the quota loop can't fire a phantom alert.
    frames = itertools.cycle([_frame(7, 78, 0), _frame(7, 2, 0)])
    monkeypatch.setattr(
        _usage_window, "_capture_with_scrollback", _capture_returning(frames)
    )

    info = await _usage_window._poll_usage_modal("@1")

    assert info is None


@pytest.mark.asyncio
async def test_codex_status_uses_parked_prompt(monkeypatch: pytest.MonkeyPatch):
    frames = iter(
        [
            "OpenAI Codex\n›",
            "5h limit: 80% left\nWeekly limit: 60% left",
            "5h limit: 80% left\nWeekly limit: 60% left",
        ]
    )
    monkeypatch.setattr(
        _usage_window, "_capture_with_scrollback", _capture_returning(frames)
    )

    info = await _usage_window._poll_codex_status("@2")

    assert info is not None
    assert info.five_hour is not None
    assert info.five_hour.used_percent == 20
    assert info.weekly is not None
    assert info.weekly.used_percent == 40


@pytest.mark.asyncio
async def test_codex_status_never_advances_sign_in_screen(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        _usage_window,
        "_capture_with_scrollback",
        _capture_returning(iter(["Sign in with ChatGPT\nSign in with Device Code"])),
    )

    info = await _usage_window._poll_codex_status("@2")

    assert info is None


@pytest.mark.asyncio
async def test_codex_status_retries_after_refresh_request(
    monkeypatch: pytest.MonkeyPatch,
):
    frames = iter(
        [
            "OpenAI Codex\n›",
            "Limits: refresh requested; run /status again shortly.",
            "Weekly limit: 88% left",
            "Weekly limit: 88% left",
        ]
    )
    monkeypatch.setattr(
        _usage_window, "_capture_with_scrollback", _capture_returning(frames)
    )

    info = await _usage_window._poll_codex_status("@2")

    assert info is not None
    assert info.weekly is not None
    assert info.weekly.used_percent == 12


@pytest.mark.asyncio
async def test_codex_status_retries_provider_stale_warning(
    monkeypatch: pytest.MonkeyPatch,
):
    frames = iter(
        [
            "OpenAI Codex\n›",
            (
                "Weekly limit: 86% left (resets 18:04 on 6 Aug)\n"
                "Warning: limits may be stale - run /status again"
            ),
            "Weekly limit: 98% left (resets 10:11 on 8 Aug)",
            "Weekly limit: 98% left (resets 10:11 on 8 Aug)",
        ]
    )
    monkeypatch.setattr(
        _usage_window, "_capture_with_scrollback", _capture_returning(frames)
    )

    info = await _usage_window._poll_codex_status("@2")

    assert info is not None
    assert info.weekly is not None
    assert info.weekly.used_percent == 2
