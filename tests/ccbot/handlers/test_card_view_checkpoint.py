"""A selected answer survives the normal state checkpoint and restart."""

from unittest.mock import AsyncMock

import pytest

from ccbot import session as session_module
from ccbot.handlers import card_carrier, card_registry, card_terminal
from ccbot.handlers.card_layout import _render_card
from ccbot.handlers.card_types import CardState, Event
from ccbot.session import Session, SessionManager


@pytest.mark.asyncio
async def test_checkpoint_restores_selected_content_after_history_shifts(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(session_module.config, "config_dir", tmp_path)
    monkeypatch.setattr(session_module.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(
        session_module.config, "summary_cache_file", tmp_path / "summaries.json"
    )
    manager = SessionManager()
    sess = Session(id="view", name="view", workdir="/tmp", backend="codex")
    manager.sessions[sess.id] = sess
    manager.active_sessions[42] = sess.id
    manager.card_msg_id[42] = 77
    monkeypatch.setattr(card_carrier, "session_manager", manager)
    monkeypatch.setattr(session_module, "session_manager", manager)
    events = [
        Event(type="final_text", text=f"ANSWER {n}", started_at=n, is_page_break=True)
        for n in (1, 2, 3)
    ]
    state = CardState(msg_id=77, events=events, current_page_idx=1)
    card_registry._cards[(42, sess.id)] = state
    manager.save_state()
    card_registry._cards.clear()
    restored_manager = SessionManager()
    monkeypatch.setattr(card_carrier, "session_manager", restored_manager)
    monkeypatch.setattr(session_module, "session_manager", restored_manager)

    async def seed(uid, session, restored):
        # Old history was trimmed; raw numeric indices now refer to another page.
        restored.events = events[1:]

    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(card_terminal, "sync_card_identity", AsyncMock())
    monkeypatch.setattr(card_carrier, "_register_msg", lambda *args: None)
    monkeypatch.setattr(
        card_carrier,
        "_legacy",
        lambda name: {
            "_ensure_seeded": seed,
            "_render_card": _render_card,
            "_edit_card": edit,
            "build_footer_keyboard": lambda *args, **kw: None,
        }[name],
    )
    try:
        assert await card_carrier.restore_card(object(), 42, sess, 77)
        painted = edit.await_args.kwargs["text"]
        assert "ANSWER 2" in painted
        assert "ANSWER 3" not in painted
    finally:
        card_registry._cards.clear()


@pytest.mark.asyncio
async def test_page_clicks_write_only_at_existing_checkpoint(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from ccbot.bot.callbacks import footer
    from ccbot.handlers import card_surface
    from ccbot.handlers.callback_data import CB_PG_NEXT

    monkeypatch.setattr(session_module.config, "state_file", tmp_path / "state.json")
    monkeypatch.setattr(
        session_module.config, "summary_cache_file", tmp_path / "summary.json"
    )
    manager = SessionManager()
    sess = Session(id="disk", name="disk", workdir="/tmp")
    manager.active_sessions[42] = sess.id
    manager.sessions[sess.id] = sess
    manager.card_msg_id[42] = 7
    state = CardState(
        msg_id=7,
        current_page_idx=0,
        events=[
            Event("final_text", f"PART {n}", 1, is_page_break=True) for n in (1, 2, 3)
        ],
    )
    card_registry._cards[(42, sess.id)] = state
    monkeypatch.setattr(footer, "session_manager", manager)
    monkeypatch.setattr(card_surface, "session_manager", manager)
    monkeypatch.setattr(footer, "refresh_panel", card_surface.refresh_panel)
    edit = AsyncMock(return_value=True)
    monkeypatch.setattr(
        card_surface,
        "_legacy",
        lambda name: {"_render_card": _render_card, "_edit_card": edit}[name],
    )
    try:
        manager.save_state()
        path = tmp_path / "state.json"
        original = path.read_bytes()
        original_mtime = path.stat().st_mtime_ns
        for _ in range(10):
            assert await footer.handle(
                SimpleNamespace(data=CB_PG_NEXT, answer=AsyncMock()),
                SimpleNamespace(bot=object()),
                SimpleNamespace(id=42),
            )
        assert "PART 2" in edit.await_args.kwargs["text"]
        assert path.read_bytes() == original
        assert path.stat().st_mtime_ns == original_mtime
        manager.save_state()
        loaded = SessionManager()
        from ccbot.handlers.card_view_checkpoint import restore_view

        recovered = CardState(msg_id=7, events=list(state.events))
        restore_view(loaded, 42, sess.id, recovered)
        assert "PART 2" in _render_card(sess, recovered, user_id=42)
        assert path.read_bytes() != original
    finally:
        card_registry._cards.clear()
