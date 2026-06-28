"""Pending-text stash freshness guard (regression for the 2026-06-28 misroute).

A message typed while no active session existed is held in ``user_data``
and forwarded once a session is created. ``user_data`` lives in memory and
is only cleared by a bot restart, so without an expiry a stash could
survive for hours and then be injected into an unrelated session created
much later — exactly how a 01:05 "smartphone 100%" message resurfaced in a
10:34 session that then got auto-named "medical insurance".

These tests pin the TTL contract of ``stash_pending_text`` /
``take_pending_text``.
"""

import time

from ccbot.handlers.directory_browser import (
    PENDING_TEXT_KEY,
    PENDING_TEXT_TTL_S,
    stash_pending_text,
    take_pending_text,
)


def test_round_trip_fresh() -> None:
    ud: dict = {}
    stash_pending_text(ud, "hello world")
    assert take_pending_text(ud) == "hello world"
    # Slot is cleared after taking — a leaked stash must never re-fire.
    assert PENDING_TEXT_KEY not in ud
    assert take_pending_text(ud) is None


def test_stale_is_dropped() -> None:
    ud: dict = {}
    stash_pending_text(ud, "9.5h old night message")
    # Backdate the stamp past the TTL.
    ud[PENDING_TEXT_KEY]["ts"] = time.time() - (PENDING_TEXT_TTL_S + 60)
    assert take_pending_text(ud) is None
    # Even though dropped, the slot is still cleared.
    assert PENDING_TEXT_KEY not in ud


def test_just_within_ttl_survives() -> None:
    ud: dict = {}
    stash_pending_text(ud, "recent")
    ud[PENDING_TEXT_KEY]["ts"] = time.time() - (PENDING_TEXT_TTL_S - 30)
    assert take_pending_text(ud) == "recent"


def test_custom_max_age() -> None:
    ud: dict = {}
    stash_pending_text(ud, "x")
    ud[PENDING_TEXT_KEY]["ts"] = time.time() - 5
    assert take_pending_text(ud, max_age_s=1) is None


def test_no_expiry_when_max_age_none() -> None:
    ud: dict = {}
    stash_pending_text(ud, "x")
    ud[PENDING_TEXT_KEY]["ts"] = time.time() - 99999
    assert take_pending_text(ud, max_age_s=None) == "x"


def test_legacy_bare_string_tolerated() -> None:
    # State in flight across the deploy that introduces the dict format.
    ud: dict = {PENDING_TEXT_KEY: "legacy text"}
    assert take_pending_text(ud) == "legacy text"
    assert PENDING_TEXT_KEY not in ud


def test_absent_and_empty() -> None:
    assert take_pending_text(None) is None
    assert take_pending_text({}) is None
    assert (
        take_pending_text({PENDING_TEXT_KEY: {"text": "", "ts": time.time()}}) is None
    )


def test_stash_none_user_data_is_noop() -> None:
    # Must not raise when user_data is unavailable.
    stash_pending_text(None, "x")
