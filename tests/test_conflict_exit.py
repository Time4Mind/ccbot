"""Tests for the sustained-``Conflict`` exit path in ``_error_handler``.

Telegram's getUpdates is exclusive per token: a sustained ``Conflict``
means a second poller is running and retry can never recover. The handler
must EXIT on a sustained storm (so the flock + supervisor converge on one
bot) yet TOLERATE a single transient Conflict from a normal restart overlap.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from telegram.error import Conflict, NetworkError

from ccbot.bot import app


@pytest.fixture(autouse=True)
def _reset_conflict_state() -> Iterator[None]:
    """Each test starts from a clean detector + a patched terminate hook."""
    app._conflict_streak = 0
    app._conflict_first_seen = None
    app._last_network_err_text = None
    app._network_first_seen = None
    app._network_last_seen = None
    yield
    app._conflict_streak = 0
    app._conflict_first_seen = None
    app._last_network_err_text = None
    app._network_first_seen = None
    app._network_last_seen = None


def _ctx(err: Exception) -> SimpleNamespace:
    """Minimal stand-in for ``ContextTypes.DEFAULT_TYPE`` — only ``.error``."""
    return SimpleNamespace(error=err)


@pytest.mark.asyncio
async def test_single_conflict_does_not_terminate(monkeypatch: pytest.MonkeyPatch):
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    await app._error_handler(None, _ctx(Conflict("conflict")))

    terminate.assert_not_called()
    assert app._conflict_streak == 1


@pytest.mark.asyncio
async def test_streak_threshold_terminates(monkeypatch: pytest.MonkeyPatch):
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    for _ in range(app.CONFLICT_MAX_STREAK):
        await app._error_handler(None, _ctx(Conflict("conflict")))

    terminate.assert_called_once()
    assert app._conflict_streak == app.CONFLICT_MAX_STREAK


@pytest.mark.asyncio
async def test_elapsed_threshold_terminates(monkeypatch: pytest.MonkeyPatch):
    """A long-running storm trips on elapsed time even below the streak cap."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 1000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    await app._error_handler(None, _ctx(Conflict("conflict")))
    terminate.assert_not_called()

    # Same Conflict still firing well past the time budget.
    clock["t"] += app.CONFLICT_MAX_SECONDS + 1.0
    await app._error_handler(None, _ctx(Conflict("conflict")))
    terminate.assert_called_once()


@pytest.mark.asyncio
async def test_non_conflict_cycle_resets_streak(monkeypatch: pytest.MonkeyPatch):
    """Two Conflicts split by a network error never reach the threshold."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    await app._error_handler(None, _ctx(Conflict("c1")))
    assert app._conflict_streak == 1

    await app._error_handler(None, _ctx(NetworkError("blip")))
    assert app._conflict_streak == 0
    assert app._conflict_first_seen is None

    await app._error_handler(None, _ctx(Conflict("c2")))
    assert app._conflict_streak == 1
    terminate.assert_not_called()


@pytest.mark.asyncio
async def test_terminate_calls_stop_running_then_exits(
    monkeypatch: pytest.MonkeyPatch,
):
    """The terminate hook stops the app cleanly, then hard-exits non-zero."""
    fake_app = MagicMock()
    monkeypatch.setattr(app, "_conflict_app", fake_app)

    exits: list[int] = []
    monkeypatch.setattr(app.os, "_exit", lambda code: exits.append(code))

    app._terminate_for_sustained_conflict()

    fake_app.stop_running.assert_called_once()
    assert exits == [1]


@pytest.mark.asyncio
async def test_terminate_exits_even_if_stop_running_raises(
    monkeypatch: pytest.MonkeyPatch,
):
    """A wedged event loop must not block the hard exit."""
    fake_app = MagicMock()
    fake_app.stop_running.side_effect = RuntimeError("loop not running")
    monkeypatch.setattr(app, "_conflict_app", fake_app)

    exits: list[int] = []
    monkeypatch.setattr(app.os, "_exit", lambda code: exits.append(code))

    app._terminate_for_sustained_conflict()

    assert exits == [1]


@pytest.mark.asyncio
async def test_single_network_error_does_not_terminate(
    monkeypatch: pytest.MonkeyPatch,
):
    """A lone transient network blip is tolerated — no exit."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    await app._error_handler(None, _ctx(NetworkError("blip")))

    terminate.assert_not_called()


@pytest.mark.asyncio
async def test_sustained_network_outage_terminates(monkeypatch: pytest.MonkeyPatch):
    """Contiguous network errors past the budget exit so Docker respawns."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 5000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    # First error starts the outage clock; not yet over budget.
    await app._error_handler(None, _ctx(NetworkError("down")))
    terminate.assert_not_called()

    # Still failing, each cycle within NETWORK_GAP_SECONDS (contiguous),
    # so elapsed accumulates. In production the first terminate() hard-exits;
    # the mock cannot, so stop feeding errors the moment it fires.
    for _ in range(100):
        if terminate.called:
            break
        clock["t"] += app.NETWORK_GAP_SECONDS
        await app._error_handler(None, _ctx(NetworkError("down")))

    terminate.assert_called_once()


@pytest.mark.asyncio
async def test_network_gap_resets_outage_clock(monkeypatch: pytest.MonkeyPatch):
    """A quiet gap (poll recovered) restarts the clock — blips never add up."""
    terminate = MagicMock()
    monkeypatch.setattr(app, "_terminate_for_sustained_conflict", terminate)

    clock = {"t": 9000.0}
    monkeypatch.setattr(app.time, "monotonic", lambda: clock["t"])

    # Blips far apart in time, each separated by a gap > NETWORK_GAP_SECONDS:
    # each is treated as a fresh outage, so elapsed never reaches the budget.
    for _ in range(10):
        await app._error_handler(None, _ctx(NetworkError("blip")))
        clock["t"] += app.NETWORK_GAP_SECONDS + app.NETWORK_MAX_SECONDS

    terminate.assert_not_called()
