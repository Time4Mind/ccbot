"""Tests for the kb-mode teardown debounce (status_polling).

terminal_parser detection over a live TUI flickers — a redraw frame, a
partial pane capture, or the cursor moving onto a ``❯ Submit`` action line
can drop an AskUserQuestion match for a single poll. Tearing kb-mode down
and clearing the pending prompt on that single miss made the inline
keyboard vanish (and let the stall-rescue misfire) while the prompt was
still on screen. ``_reconcile_no_ui_state`` now requires
``KB_CLEAR_CONFIRM_POLLS`` CONSECUTIVE no-UI polls before clearing, and
``_surface_new_interactive_ui`` resets the streak whenever the prompt is
re-detected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import ccbot.handlers.notifications as notifications
from ccbot.handlers import status_polling
from ccbot.handlers.status_polling import (
    AUTO_APPROVE_MAX_ATTEMPTS,
    KB_CLEAR_CONFIRM_POLLS,
    _auto_approve_attempts,
    _kb_clear_miss,
    _maybe_auto_approve,
    _reconcile_no_ui_state,
    _surface_new_interactive_ui,
)
from ccbot.session_models import Session


@pytest.fixture(autouse=True)
def _clear_streak():
    _kb_clear_miss.clear()
    _auto_approve_attempts.clear()
    yield
    _kb_clear_miss.clear()
    _auto_approve_attempts.clear()


def _sess(sid: str = "s1") -> Session:
    return Session(id=sid, name="t", window_id="@1", workdir="/tmp", state="active")


@pytest.mark.asyncio
async def test_single_miss_does_not_clear_kb():
    """One no-UI poll with a pending prompt must NOT tear kb-mode down."""
    assert KB_CLEAR_CONFIRM_POLLS >= 2  # premise
    sess = _sess()
    bot = AsyncMock()
    exit_kb = AsyncMock()

    with (
        patch.object(notifications, "has_pending_kb", lambda u, s: (True, True)),
        patch.object(notifications, "exit_kb_mode", exit_kb),
    ):
        await _reconcile_no_ui_state(
            bot, 1, sess.window_id, "", sess, is_bg_session=False
        )

    exit_kb.assert_not_called()
    assert _kb_clear_miss[(1, sess.id)] == 1


@pytest.mark.asyncio
async def test_two_consecutive_misses_clear_kb():
    """The teardown fires only on the KB_CLEAR_CONFIRM_POLLS-th miss."""
    sess = _sess()
    bot = AsyncMock()
    exit_kb = AsyncMock()

    with (
        patch.object(notifications, "has_pending_kb", lambda u, s: (True, True)),
        patch.object(notifications, "exit_kb_mode", exit_kb),
    ):
        for _ in range(KB_CLEAR_CONFIRM_POLLS):
            await _reconcile_no_ui_state(
                bot, 1, sess.window_id, "", sess, is_bg_session=False
            )

    exit_kb.assert_called_once_with(bot, 1, sess, clear_pending=True)
    # Streak reset after firing so a later prompt starts fresh.
    assert (1, sess.id) not in _kb_clear_miss


@pytest.mark.asyncio
async def test_pending_gone_resets_streak():
    """If the prompt is cleared elsewhere, the miss streak resets so a new
    prompt's first flicker doesn't immediately trip the threshold."""
    sess = _sess()
    bot = AsyncMock()
    exit_kb = AsyncMock()

    with (
        patch.object(notifications, "exit_kb_mode", exit_kb),
    ):
        # One miss with a pending prompt → streak 1.
        with patch.object(notifications, "has_pending_kb", lambda u, s: (True, True)):
            await _reconcile_no_ui_state(
                bot, 1, sess.window_id, "", sess, is_bg_session=False
            )
        assert _kb_clear_miss[(1, sess.id)] == 1

        # Prompt no longer pending → streak cleared.
        with patch.object(notifications, "has_pending_kb", lambda u, s: (False, False)):
            await _reconcile_no_ui_state(
                bot, 1, sess.window_id, "", sess, is_bg_session=False
            )
        assert (1, sess.id) not in _kb_clear_miss

        # A new prompt's first flicker is only miss #1, not an instant clear.
        with patch.object(notifications, "has_pending_kb", lambda u, s: (True, True)):
            await _reconcile_no_ui_state(
                bot, 1, sess.window_id, "", sess, is_bg_session=False
            )

    exit_kb.assert_not_called()
    assert _kb_clear_miss[(1, sess.id)] == 1


@pytest.mark.asyncio
async def test_redetect_resets_miss_streak():
    """Re-detecting the prompt (surface → enter_kb_mode) resets the streak,
    so an earlier flicker doesn't count toward a future teardown."""
    sess = _sess()
    bot = AsyncMock()
    _kb_clear_miss[(1, sess.id)] = 1  # an earlier flicker already counted

    content_obj = MagicMock()
    content_obj.content = "❯ Submit"
    content_obj.name = "AskUserQuestion"
    enter_kb = AsyncMock()

    with (
        patch.object(
            status_polling, "_maybe_auto_approve", AsyncMock(return_value=False)
        ),
        patch.object(
            status_polling, "extract_interactive_content", lambda p: content_obj
        ),
        patch.object(notifications, "enter_kb_mode", enter_kb),
    ):
        handled = await _surface_new_interactive_ui(
            bot,
            1,
            "@1",
            "pane",
            sess,
            is_bg_session=False,
            interactive_window=None,
        )

    assert handled is True
    enter_kb.assert_awaited_once()
    assert (1, sess.id) not in _kb_clear_miss  # streak reset on re-detect


# --- auto-approve escalation (fix #2) -------------------------------------

_YESNO_PANE = "  Do you want to proceed?\n❯ 1. Yes\n  2. No, tell Claude\n"


def _content(name="PermissionPrompt", body="fetch example.com"):
    obj = MagicMock()
    obj.name = name
    obj.content = body
    return obj


@pytest.mark.asyncio
async def test_auto_approve_escalates_after_max_attempts():
    """A prompt that survives the auto-Yes keystroke stops being re-pressed
    after AUTO_APPROVE_MAX_ATTEMPTS and returns False so the caller can
    surface the manual keyboard."""
    send_keys = AsyncMock()
    with (
        patch.object(
            status_polling.session_manager,
            "get_user_settings",
            lambda u: {"auto_approve": "on"},
        ),
        patch.object(status_polling.tmux_manager, "send_keys", send_keys),
        patch.object(
            status_polling, "extract_interactive_content", lambda p: _content()
        ),
    ):
        # First MAX attempts: keystroke sent, returns True.
        for i in range(AUTO_APPROVE_MAX_ATTEMPTS):
            assert await _maybe_auto_approve(1, "@1", _YESNO_PANE) is True
        assert send_keys.await_count == AUTO_APPROVE_MAX_ATTEMPTS
        # Same prompt still up next poll → give up: no new keystroke, False.
        assert await _maybe_auto_approve(1, "@1", _YESNO_PANE) is False
        assert send_keys.await_count == AUTO_APPROVE_MAX_ATTEMPTS  # unchanged
        # Stays escalated on subsequent polls.
        assert await _maybe_auto_approve(1, "@1", _YESNO_PANE) is False
        assert send_keys.await_count == AUTO_APPROVE_MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_auto_approve_distinct_prompts_never_escalate():
    """Different prompts (distinct signatures) each get a one-shot auto-Yes;
    the counter resets per prompt so legitimate approvals aren't throttled."""
    send_keys = AsyncMock()
    bodies = iter([f"fetch host-{i}.com" for i in range(10)])
    with (
        patch.object(
            status_polling.session_manager,
            "get_user_settings",
            lambda u: {"auto_approve": "on"},
        ),
        patch.object(status_polling.tmux_manager, "send_keys", send_keys),
        patch.object(
            status_polling,
            "extract_interactive_content",
            lambda p: _content(body=next(bodies)),
        ),
    ):
        for _ in range(AUTO_APPROVE_MAX_ATTEMPTS + 3):
            assert await _maybe_auto_approve(1, "@1", _YESNO_PANE) is True
    assert send_keys.await_count == AUTO_APPROVE_MAX_ATTEMPTS + 3


@pytest.mark.asyncio
async def test_auto_approve_counter_resets_when_prompt_clears():
    """Once the prompt clears, _reconcile_no_ui_state pops the escalation
    counter so a later prompt starts fresh instead of instantly escalating."""
    send_keys = AsyncMock()
    with (
        patch.object(
            status_polling.session_manager,
            "get_user_settings",
            lambda u: {"auto_approve": "on"},
        ),
        patch.object(status_polling.tmux_manager, "send_keys", send_keys),
        patch.object(
            status_polling, "extract_interactive_content", lambda p: _content()
        ),
    ):
        # Escalate.
        for _ in range(AUTO_APPROVE_MAX_ATTEMPTS + 1):
            await _maybe_auto_approve(1, "@1", _YESNO_PANE)
        assert (1, "@1") in _auto_approve_attempts

        # Prompt gone (empty pane → no interactive UI) → counter popped.
        bot = AsyncMock()
        with patch.object(notifications, "has_pending_kb", lambda u, s: (False, False)):
            await _reconcile_no_ui_state(bot, 1, "@1", "", _sess(), is_bg_session=False)
        assert (1, "@1") not in _auto_approve_attempts

        # A fresh prompt gets auto-approved again (not instantly escalated).
        assert await _maybe_auto_approve(1, "@1", _YESNO_PANE) is True
