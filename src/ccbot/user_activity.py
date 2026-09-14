"""Process-local user-activity clock for adaptive live-card coalescing."""

from __future__ import annotations

import time


_DEFAULT_ADAPTIVE_LAG = 4.0
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
    """Increase the default card lag as explicit user inactivity grows.

    Custom lag choices retain their existing fixed behavior. Only the current
    default of four seconds gets the adaptive 4/6/10/20-second schedule.
    """
    base = max(0.0, float(configured_lag))
    if base != _DEFAULT_ADAPTIVE_LAG:
        return base
    current = time.monotonic() if now is None else now
    idle = max(0.0, current - last_seen(user_id))
    if idle >= 65 * 60:
        return 20.0
    if idle >= 35 * 60:
        return 10.0
    if idle >= 15 * 60:
        return 6.0
    return _DEFAULT_ADAPTIVE_LAG


def reset_for_test(*, started_at: float | None = None) -> None:
    """Reset module state between tests."""
    global _started_at
    _last_user_activity.clear()
    _started_at = time.monotonic() if started_at is None else started_at
