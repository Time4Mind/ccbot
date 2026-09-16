"""Regression tests for kb-mode surfacing when the carrier is on a menu.

Bug: when the user has the live card on Menu / List / Settings /
History (``state.in_menu_view=True``) and claude emits an interactive
prompt (AskUserQuestion / ExitPlanMode / permission), the kb-mode
keyboard could remain hidden behind the menu. Root cause:
``enter_kb_mode`` → ``_edit_card`` short-circuits with ``return True``
when ``state.in_menu_view`` is set, leaving the menu screen visible.

Fix: ``enter_kb_mode`` clears ``in_menu_view`` before painting so
``_edit_card`` actually edits; ``_should_buffer`` then keeps
``in_kb_mode`` as a buffer reason so stray streaming events don't
overwrite the kb keyboard before the user acts.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import (
    CardState,
    _card_locks,
    _cards,
    _repost_intent,
    _should_buffer,
    enter_kb_mode,
)
from ccbot.handlers.kb_mode import build_kb_mode_keyboard
from ccbot.session_models import Session
from ccbot.session_monitor import NewMessage


@pytest.fixture(autouse=True)
def _clear_card_state():
    _cards.clear()
    _card_locks.clear()
    _repost_intent.clear()
    yield
    _cards.clear()
    _card_locks.clear()
    _repost_intent.clear()


def _make_sess(sid: str = "s1") -> Session:
    return Session(
        id=sid,
        name="test",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="uuid-" + sid,
    )


@pytest.mark.asyncio
async def test_enter_kb_mode_clears_menu_view(monkeypatch):
    """``enter_kb_mode`` MUST clear ``in_menu_view`` before editing the
    card, otherwise ``_edit_card`` short-circuits and the kb keyboard
    never surfaces — the user previously had to tap Shot to unstick it.
    """
    sess = _make_sess()
    bot = AsyncMock()
    edits: list[dict] = []
    sends: list[dict] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        sends.append({"text": text, "reply_markup": reply_markup})
        st.msg_id = 1234

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        edits.append({"text": text, "reply_markup": reply_markup})
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)

    # Carrier exists AND user is on a Menu screen — the exact failure
    # configuration the user reported.
    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = 999
    state.in_menu_view = True

    await enter_kb_mode(bot, 42, sess, "Which file should I edit?", "AskUserQuestion")

    assert state.in_menu_view is False, (
        "in_menu_view must be cleared on kb-mode entry — otherwise _edit_card "
        "short-circuits and the kb keyboard never appears"
    )
    assert state.in_kb_mode is True
    assert state.kb_prompt == "Which file should I edit?"
    assert len(edits) == 1, "kb-mode entry must edit the existing carrier"
    assert len(sends) == 0, "should edit, not spawn a new card"
    # And the keyboard passed to _edit_card must be the kb keyboard,
    # NOT the default footer.
    assert edits[0]["reply_markup"] is not None


@pytest.mark.asyncio
async def test_enter_kb_mode_spawns_when_no_carrier(monkeypatch):
    """When ``msg_id is None``, ``enter_kb_mode`` spawns via ``_send_card``."""
    sess = _make_sess()
    bot = AsyncMock()
    sends: list[dict] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        sends.append({"text": text, "reply_markup": reply_markup})
        st.msg_id = 5678

    async def fake_edit_card(b, uid, st, *, text, reply_markup=None):
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_edit_card", fake_edit_card)

    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = None
    state.in_menu_view = True

    await enter_kb_mode(bot, 42, sess, "Choose option:", "AskUserQuestion")

    assert len(sends) == 1
    assert state.msg_id == 5678
    assert state.in_kb_mode is True


def test_should_buffer_blocks_on_in_kb_mode():
    """Once kb-mode is active, regular claude events must buffer so
    ``update_session_card`` doesn't repaint over the kb keyboard with
    the default footer.
    """
    state = CardState()
    state.in_kb_mode = True
    state.in_menu_view = False  # menu wasn't the trigger
    fake_active = MagicMock()
    fake_active.id = "s1"
    import ccbot.session as session_mod

    original = session_mod.session_manager.get_active_session
    session_mod.session_manager.get_active_session = lambda uid: fake_active
    try:
        assert _should_buffer(42, "s1", state) is True
    finally:
        session_mod.session_manager.get_active_session = original


def test_should_buffer_does_not_block_when_idle():
    """Sanity: with no menu / no kb / no repost intent and session is
    active, _should_buffer returns False (the normal render path).
    """
    state = CardState()
    fake_active = MagicMock()
    fake_active.id = "s1"
    import ccbot.session as session_mod

    original = session_mod.session_manager.get_active_session
    session_mod.session_manager.get_active_session = lambda uid: fake_active
    try:
        assert _should_buffer(42, "s1", state) is False
    finally:
        session_mod.session_manager.get_active_session = original


def _button_rows(keyboard):
    return [[button.text for button in row] for row in keyboard.inline_keyboard]


def test_model_picker_uses_one_button_per_native_option(
    sample_pane_settings: str,
) -> None:
    keyboard = build_kb_mode_keyboard(
        42, "@5", ui_name="Settings", prompt_content=sample_pane_settings
    )

    assert _button_rows(keyboard) == [
        ["Default"],
        ["✓ Sonnet"],
        ["Haiku"],
        ["× Cancel"],
    ]
    callbacks = [row[0].callback_data for row in keyboard.inline_keyboard]
    assert callbacks[:3] == [
        "aq:pick:0:@5",
        "aq:pick:1:@5",
        "aq:pick:2:@5",
    ]
    assert callbacks[3] == "aq:esc:@5"


def test_effort_picker_marks_current_value_not_cursor() -> None:
    prompt = (
        "Select Reasoning Level for gpt-5.6-sol\n"
        "\n"
        "› 1. Low (default)     Fast responses with lighter reasoning\n"
        "  2. Medium (current)  Balances speed and reasoning depth\n"
        "  3. High              Greater reasoning depth\n"
        "\n"
        "Press enter to confirm or esc to go back"
    )

    keyboard = build_kb_mode_keyboard(
        42, "@5", ui_name="Settings", prompt_content=prompt
    )

    assert _button_rows(keyboard) == [
        ["Low"],
        ["✓ Medium"],
        ["High"],
        ["× Cancel"],
    ]


def test_non_model_settings_picker_keeps_navigation_grid() -> None:
    keyboard = build_kb_mode_keyboard(
        42,
        "@5",
        ui_name="Settings",
        prompt_content="Select output style\n  1. Concise\n  2. Detailed\nEsc to cancel",
    )

    assert _button_rows(keyboard)[0] == ["␣ Space", "↑", "⇥ Tab"]


@pytest.mark.asyncio
async def test_repost_card_preserves_native_picker_keyboard(monkeypatch) -> None:
    """A carrier repost while /model is open must keep its option buttons."""
    sess = _make_sess()
    bot = AsyncMock()
    bot.delete_message = AsyncMock()
    prompt = (
        "Select model\n\n"
        "  1. Default\n"
        "› 2. Sonnet (current)\n"
        "  3. Haiku\n\n"
        "Press enter to confirm or esc to go back"
    )
    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = 999
    state.in_kb_mode = True
    state.kb_prompt = prompt
    state.kb_ui_name = "Settings"
    sends: list[object] = []

    async def fake_send_card(b, uid, s, st, *, text, reply_markup=None):
        sends.append(reply_markup)
        st.msg_id = 1000
        return True

    monkeypatch.setattr(notifications, "_send_card", fake_send_card)
    monkeypatch.setattr(notifications, "_ensure_seeded", AsyncMock(return_value=None))
    monkeypatch.setattr(notifications, "_render_card", lambda *args, **kwargs: "card")

    await notifications.repost_card(bot, 42, sess)

    assert len(sends) == 1
    assert sends[0] is not None
    assert _button_rows(sends[0]) == [
        ["Default"],
        ["✓ Sonnet"],
        ["Haiku"],
        ["× Cancel"],
    ]


@pytest.mark.asyncio
async def test_transcript_event_does_not_close_live_picker(monkeypatch) -> None:
    """Transcript growth is not proof that the terminal selector disappeared."""
    from ccbot.bot import session_events

    sess = _make_sess()
    state = _cards.setdefault((42, sess.id), CardState())
    state.msg_id = 999
    state.in_kb_mode = True
    state.kb_prompt = "Select model\n› 1. Sonnet (current)\n  2. Haiku"
    state.kb_ui_name = "Settings"

    monkeypatch.setattr(
        session_events.session_manager,
        "all_user_sessions_with_claude_id",
        lambda _sid: [(42, sess)],
    )
    monkeypatch.setattr(
        session_events.session_manager, "touch_session", lambda _sid: None
    )
    monkeypatch.setattr(session_events, "is_active_for_user", lambda *_args: True)
    monkeypatch.setattr(session_events, "fire_typing", AsyncMock())
    monkeypatch.setattr(session_events, "update_session_card", AsyncMock())
    exit_kb = AsyncMock()
    monkeypatch.setattr(notifications, "exit_kb_mode", exit_kb)

    msg = NewMessage(
        session_id=sess.claude_session_id or "",
        text="background progress",
        is_complete=False,
        content_type="text",
        role="assistant",
        stop_reason=None,
    )
    await session_events.handle_new_message(msg, AsyncMock())

    exit_kb.assert_not_awaited()
    session_events.update_session_card.assert_awaited_once()
