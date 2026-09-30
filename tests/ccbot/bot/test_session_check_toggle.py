"""Session actions toggle/acknowledge checks without changing worker lifecycle."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.bot import callbacks as dispatcher
from ccbot.bot.callbacks import footer, more_menu, switcher
from ccbot.handlers import bg_status, notifications
from ccbot.handlers.callback_data import CB_FT_MORE, CB_PG_NEXT, CB_SW_USE
from ccbot.handlers.card_types import CardState, Event, TurnPhase
from ccbot.handlers.switcher import build_switcher_keyboard
from ccbot.session_models import Session


@pytest.fixture
def scene(monkeypatch):
    target = Session(id="target", name="target", window_id="@1")
    other = Session(id="other", name="other")
    selected = [target]
    painted = []
    visible = []
    card = CardState(
        msg_id=99,
        turn_phase=TurnPhase.IDLE,
        current_page_idx=0,
        events=[
            Event(type="user_msg", text="first", started_at=1, is_page_break=True),
            Event(type="final_text", text="first answer", started_at=2),
            Event(type="user_msg", text="second", started_at=3, is_page_break=True),
            Event(type="final_text", text="second answer", started_at=4),
        ],
    )
    manager = switcher.session_manager
    monkeypatch.setattr(
        manager, "get_session", lambda sid: target if sid == target.id else other
    )
    monkeypatch.setattr(manager, "get_active_session", lambda _uid: selected[0])
    monkeypatch.setattr(
        manager, "list_user_sessions", lambda _uid, **_kw: [target, other]
    )
    monkeypatch.setattr(manager, "get_user_settings", lambda _uid: {})
    monkeypatch.setattr(manager, "save_state", lambda: None)
    monkeypatch.setattr(
        "ccbot.bot._new_session_flow.cancel_for_active_card", lambda *_a: False
    )
    for module in (switcher, dispatcher, footer):
        monkeypatch.setattr(module, "get_card_state", lambda *_a: card)
    monkeypatch.setitem(notifications._cards, (42, target.id), card)
    monkeypatch.setattr(dispatcher, "is_user_allowed", lambda _u: True)
    monkeypatch.setattr(switcher, "_strip_orphan_switcher_if_current", AsyncMock())

    def label():
        keyboard = build_switcher_keyboard(42)
        return next(
            b.text
            for row in keyboard.inline_keyboard
            for b in row
            if b.callback_data == CB_SW_USE + target.id
        )

    async def activate(_uid, _old, _new, _msg):
        selected[0] = target
        return None

    async def paint(*_a, **_kw):
        painted.append(label())
        visible.append("card")
        return True

    async def refresh_keyboard(*_a, **_kw):
        if card.in_menu_view:
            return False
        return await paint()

    async def set_view(*_a, **_kw):
        visible.append("menu")

    monkeypatch.setattr(switcher, "activate_card_on_carrier", activate)
    monkeypatch.setattr(switcher, "paint_card_on_carrier", paint)
    monkeypatch.setattr(footer, "refresh_panel", paint)
    monkeypatch.setattr(dispatcher, "refresh_panel", paint)
    monkeypatch.setattr(
        dispatcher, "refresh_session_keyboard", refresh_keyboard, raising=False
    )
    monkeypatch.setattr(footer, "set_view", set_view)
    monkeypatch.setattr(more_menu, "render_menu_text", lambda *_a, **_kw: "Menu")
    monkeypatch.setattr(more_menu, "begin_menu_refresh", lambda *_a: None)
    context = SimpleNamespace(bot=AsyncMock(), user_data={})

    async def tap(data=CB_SW_USE + target.id):
        query = SimpleNamespace(
            data=data, message=SimpleNamespace(message_id=99), answer=AsyncMock()
        )
        await dispatcher.callback_handler(
            SimpleNamespace(
                callback_query=query, effective_user=SimpleNamespace(id=42)
            ),
            context,
        )
        return "✅" in label()

    with patch.dict(bg_status._bg, {}, clear=True):
        yield SimpleNamespace(
            target=target,
            other=other,
            selected=selected,
            card=card,
            tap=tap,
            label=label,
            visible=visible,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial",
    [
        "finished",
        "seen_finished",
        "working",
        "error",
        "needs_action",
        "stalled",
        None,
        "busy_finished",
    ],
)
async def test_active_session_taps_toggle_check_and_preserve_completion(scene, initial):
    busy = initial == "busy_finished"
    if busy:
        initial = "seen_finished"
        scene.card.turn_phase = TurnPhase.RUNNING
        scene.card.pane_busy = True
    if initial == "finished":
        scene.selected[0] = scene.other
    if initial is not None:
        bg_status.update_status(42, scene.target.id, initial)
    if busy or initial not in ("finished", "seen_finished"):
        expected = bg_status.status_emoji(42, scene.target.id)
        for _ in range(3):
            assert await scene.tap() is False
            assert bg_status.status_emoji(42, scene.target.id) == expected
            assert bg_status.get_status(42, scene.target.id) == initial
        if initial is not None and not busy:
            legacy = bg_status.serialize_per_user()
            legacy["42"][scene.target.id]["manual_checked"] = True
            bg_status.load_per_user(legacy)
            assert bg_status.status_emoji(42, scene.target.id) == expected
        return
    if initial == "finished":
        assert await scene.tap() is True  # First presentation preserves unread result.
        assert await scene.tap() is False
    lifecycle = bg_status.get_status(42, scene.target.id)
    assert await scene.tap() is True
    assert bg_status.get_status(42, scene.target.id) == lifecycle
    assert await scene.tap() is False
    assert bg_status.get_status(42, scene.target.id) == lifecycle
    assert await scene.tap() is True

    # Action in another session does not acknowledge this session's check.
    scene.selected[0] = scene.other
    await scene.tap("noop")
    assert "✅" in scene.label()
    # Re-entering the checked session is its second action and clears the check.
    assert await scene.tap() is False
    assert await scene.tap() is True
    bg_status.load_per_user(bg_status.serialize_per_user())
    assert await scene.tap() is False
    assert await scene.tap() is True
    bg_status.update_status(42, scene.target.id, "working", force=True)
    assert bg_status.status_emoji(42, scene.target.id) == "🔶"
    bg_status.update_status(42, scene.target.id, "finished")
    assert await scene.tap() is True
    assert await scene.tap() is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [CB_PG_NEXT, CB_FT_MORE])
async def test_next_session_control_acknowledges_manual_check_without_losing_view(
    scene, action
):
    bg_status.update_status(42, scene.target.id, "seen_finished")
    assert await scene.tap() is True
    assert await scene.tap(action) is False
    assert bg_status.get_status(42, scene.target.id) == "seen_finished"
    assert scene.visible[-1] == ("menu" if action == CB_FT_MORE else "card")
    assert scene.card.current_page_idx == (0 if action == CB_FT_MORE else 1)
    bg_status.load_per_user(bg_status.serialize_per_user())
    assert "✅" not in scene.label()
