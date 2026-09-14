"""Process-local user-activity clock for adaptive live-card coalescing."""

from __future__ import annotations

import time


_MIN_LIVE_LAG = 2.0
_started_at = time.monotonic()
_last_user_activity: dict[int, float] = {}


def record(user_id: int, *, now: float | None = None) -> None:
    """Record an explicit Telegram action by the user."""
    _last_user_activity[user_id] = time.monotonic() if now is None else now


def last_seen(user_id: int) -> float:
    """Return the user's latest action, or process start after a restart."""
    return _last_user_activity.get(user_id, _started_at)


def effective_live_lag(
    user_id: int,
    configured_lag: float,
    *,
    now: float | None = None,
) -> float:
    """Increase every supported card lag as explicit user inactivity grows."""
    base = max(_MIN_LIVE_LAG, float(configured_lag))
    current = time.monotonic() if now is None else now
    idle = max(0.0, current - last_seen(user_id))
    if idle >= 65 * 60:
        return base * 5.0
    if idle >= 35 * 60:
        return base * 2.5
    if idle >= 15 * 60:
        return base * 1.5
    return base


def reset_for_test(*, started_at: float | None = None) -> None:
    """Reset module state between tests."""
    global _started_at
    _last_user_activity.clear()
    _started_at = time.monotonic() if started_at is None else started_at
