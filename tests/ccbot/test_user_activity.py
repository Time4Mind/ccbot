from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from ccbot import user_activity


@pytest.fixture(autouse=True)
def _reset_activity() -> None:
    user_activity.reset_for_test(started_at=100.0)
    yield
    user_activity.reset_for_test()


@pytest.mark.parametrize(
    ("idle_seconds", "expected_lag"),
    [
        (0, 4.0),
        (899.9, 4.0),
        (900, 6.0),
        (2099.9, 6.0),
        (2100, 10.0),
        (3899.9, 10.0),
        (3900, 20.0),
    ],
)
def test_default_live_lag_adapts_to_user_inactivity(
    idle_seconds: float, expected_lag: float
) -> None:
    user_activity.record(42, now=200.0)

    assert (
        user_activity.effective_live_lag(42, 4, now=200.0 + idle_seconds)
        == expected_lag
    )


def test_any_new_user_activity_resets_adaptive_lag() -> None:
    user_activity.record(42, now=200.0)
    assert user_activity.effective_live_lag(42, 4, now=4100.0) == 20.0

    user_activity.record(42, now=4100.0)

    assert user_activity.effective_live_lag(42, 4, now=4100.0) == 4.0


@pytest.mark.parametrize("configured", [0, 2, 8])
def test_explicit_non_default_live_lag_remains_fixed(configured: int) -> None:
    user_activity.record(42, now=200.0)

    assert user_activity.effective_live_lag(42, configured, now=5000.0) == float(
        configured
    )


def test_no_user_action_uses_process_start_as_idle_anchor() -> None:
    assert user_activity.effective_live_lag(42, 4, now=1000.0) == 6.0


@pytest.mark.asyncio
async def test_message_activity_records_allowed_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot import activity

    monkeypatch.setattr(activity, "is_user_allowed", lambda _uid: True)
    update = SimpleNamespace(effective_user=SimpleNamespace(id=42))

    await activity.record_user_message_activity(update, MagicMock())

    assert (
        user_activity.effective_live_lag(42, 4, now=user_activity.last_seen(42)) == 4.0
    )


@pytest.mark.asyncio
async def test_even_noop_button_records_user_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot import callbacks

    recorded: list[int] = []
    monkeypatch.setattr(callbacks, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(callbacks, "record_user_activity", recorded.append)
    query = SimpleNamespace(data="noop", answer=AsyncMock())
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=42),
    )

    await callbacks.callback_handler(update, MagicMock())

    assert recorded == [42]
    query.answer.assert_awaited_once()
