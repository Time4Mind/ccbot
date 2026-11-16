"""Retired final messages retain their own pages without changing active work."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot import keyboard_cleanup
from ccbot.handlers.card_final_delivery import final_payload
from ccbot.handlers.card_types import CardState, Event
from ccbot.final_delivery import FinalDeliveryQueue


@pytest.mark.asyncio
async def test_completed_answer_keeps_all_pages_after_cleanup_and_restart(
    monkeypatch, tmp_path
):
    from ccbot.handlers import card_registry
    from ccbot.session import session_manager

    cleanup = keyboard_cleanup.KeyboardCleanup(tmp_path / "keyboard.json")
    monkeypatch.setattr(keyboard_cleanup, "cleanup_queue", cleanup)
    monkeypatch.setattr(
        session_manager,
        "get_session",
        lambda sid: SimpleNamespace(
            id=sid,
            name="Source",
            workdir="/tmp",
            goal="",
            backend="codex",
            node_id="local",
        ),
    )
    monkeypatch.setattr(
        session_manager, "get_user_settings", lambda uid: {"card_page_lines": 100}
    )
    state = CardState(
        events=[
            Event("final_text", f"PAGE {n}\n" + "x " * 1200, 1.0, is_page_break=True)
            for n in (1, 2, 3)
        ],
        current_page_idx=1,
    )
    queue = FinalDeliveryQueue(tmp_path / "delivery.json")
    item = queue.enqueue(42, "source", final_payload(state, state.events[0], None))
    queue.bind_ack(item.id, 7)
    queue.resolve(item)
    restarted = keyboard_cleanup.KeyboardCleanup(cleanup.path)
    monkeypatch.setattr(keyboard_cleanup, "cleanup_queue", restarted)
    restarted.load()
    bot = SimpleNamespace(edit_message_reply_markup=AsyncMock(return_value=True))
    monkeypatch.setattr(card_registry, "_stripped_switchers", {})
    monkeypatch.setattr(
        card_registry, "_switcher_carrier_is_protected", lambda *a: False
    )
    await card_registry.retire_card_keyboard(bot, 42, 7)
    kb = bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
    assert len(kb.inline_keyboard) == 2
    assert [b.text for b in kb.inline_keyboard[0]] == ["◀", "2/3", "▶"]
    assert kb.inline_keyboard[0][0].callback_data.startswith("fa:7:")
    assert kb.inline_keyboard[0][0].callback_data.endswith(":0")
    assert kb.inline_keyboard[1][0].callback_data == "sw:source"


@pytest.mark.asyncio
async def test_old_answer_page_does_not_touch_selected_session(monkeypatch, tmp_path):
    from ccbot.bot.callbacks import saved_answer
    from ccbot.handlers import message_sender
    from ccbot.session import session_manager

    cleanup = keyboard_cleanup.KeyboardCleanup(tmp_path / "keyboard.json")
    monkeypatch.setattr(keyboard_cleanup, "cleanup_queue", cleanup)
    cleanup.remember_final(
        42,
        7,
        "source",
        answer={
            "pages": ["FIRST", "SECOND", "THIRD"],
            "page": 1,
            "timestamp": 1,
            "hash": "fixture",
        },
    )
    monkeypatch.setattr(session_manager, "get_last_switcher_msg", lambda _: 99)
    active = SimpleNamespace(id="other")
    monkeypatch.setattr(session_manager, "get_active_session", lambda _: active)
    edit = AsyncMock()
    monkeypatch.setattr(message_sender, "safe_edit", edit)
    query = SimpleNamespace(
        data="fa:7:fixture:0",
        message=SimpleNamespace(message_id=7, chat=SimpleNamespace(id=42)),
        answer=AsyncMock(),
    )
    assert await saved_answer.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=42)
    )
    assert edit.await_args.args[1] == "FIRST"
    assert session_manager.get_active_session(42) is active
    # An old callback must not repaint a carrier reused for another answer.
    cleanup.rebind_final(42, 7, "other")
    edit.reset_mock()
    assert await saved_answer.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=42)
    )
    edit.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_button_cannot_replace_a_newer_answer_in_same_session(
    monkeypatch, tmp_path
):
    from ccbot.bot.callbacks import saved_answer
    from ccbot.handlers import message_sender
    from ccbot.session import session_manager

    cleanup = keyboard_cleanup.KeyboardCleanup(tmp_path / "keyboard.json")
    monkeypatch.setattr(keyboard_cleanup, "cleanup_queue", cleanup)
    cleanup.remember_final(
        42,
        7,
        "source",
        answer={"pages": ["OLD", "END"], "page": 0, "timestamp": 1, "hash": "old"},
    )
    cleanup.remember_final(
        42,
        7,
        "source",
        answer={"pages": ["NEW", "END NEW"], "page": 0, "timestamp": 2, "hash": "new"},
    )
    monkeypatch.setattr(session_manager, "get_last_switcher_msg", lambda _: 99)
    edit = AsyncMock()
    monkeypatch.setattr(message_sender, "safe_edit", edit)
    query = SimpleNamespace(
        data="fa:7:old:0",
        message=SimpleNamespace(message_id=7, chat=SimpleNamespace(id=42)),
        answer=AsyncMock(),
    )
    assert await saved_answer.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=42)
    )
    edit.assert_not_awaited()
    query.data = "fa:7:new:1"
    before = {
        p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*.json")
    }
    assert await saved_answer.handle(
        query, SimpleNamespace(bot=object()), SimpleNamespace(id=42)
    )
    assert edit.await_args.args[1] == "END NEW"
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before} == before
