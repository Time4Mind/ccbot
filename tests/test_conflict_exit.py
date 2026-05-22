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
    yield
    app._conflict_streak = 0
    app._conflict_first_seen = None
    app._last_network_err_text = None


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
