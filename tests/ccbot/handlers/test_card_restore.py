"""Tests for ``notifications.restore_card`` + ``session_manager.card_msg_id``
persistence — the live card is repainted in place after a bot restart
instead of being orphaned in chat (in-memory ``_cards`` is lost on restart)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import notifications
from ccbot.handlers.notifications import CardState, _cards, restore_card
from ccbot.session_models import Session


@pytest.fixture(autouse=True)
def _clear_card_state():
    _cards.clear()
    yield
    _cards.clear()


def _make_sess(sid: str = "s1") -> Session:
    return Session(
        id=sid,
        name="test",
        window_id="@1",
        workdir="/tmp",
        state="active",
        claude_session_id="uuid-" + sid,
    )


class TestCardMsgPersistence:
    def test_set_get_clear_roundtrip(self) -> None:
        from ccbot.session import session_manager

        try:
            session_manager.set_card_msg(4242, 777)
            assert session_manager.get_card_msg(4242) == 777
            session_manager.clear_card_msg(4242)
            assert session_manager.get_card_msg(4242) is None
        finally:
            session_manager.card_msg_id.pop(4242, None)

    def test_survives_save_load(self, tmp_path, monkeypatch) -> None:
        from ccbot.config import config
        from ccbot.session import SessionManager

        monkeypatch.setattr(config, "state_file", tmp_path / "state.json")
        mgr = SessionManager()
        mgr.set_card_msg(4242, 999)
        # Fresh manager reads the just-written state file.
        mgr2 = SessionManager()
        assert mgr2.get_card_msg(4242) == 999


@pytest.mark.asyncio
class TestRestoreCard:
    async def test_repaints_in_place_on_success(self, monkeypatch) -> None:
        monkeypatch.setattr(
            notifications, "_ensure_seeded", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(notifications, "_edit_card", AsyncMock(return_value=True))
        bot = AsyncMock()
        sess = _make_sess()
        ok = await restore_card(bot, 1, sess, 555)
        assert ok is True
        state = _cards.get((1, sess.id))
        assert state is not None
        assert state.msg_id == 555

    async def test_clears_pointer_when_message_gone(self, monkeypatch) -> None:
        from ccbot.session import session_manager

        monkeypatch.setattr(
            notifications, "_ensure_seeded", AsyncMock(return_value=None)
        )
        # Edit fails permanently → message deleted by the user.
        monkeypatch.setattr(notifications, "_edit_card", AsyncMock(return_value=False))
        # clear_card_msg is sync; use a plain stub recording the call.
        calls: list[int] = []
        monkeypatch.setattr(
            session_manager, "clear_card_msg", lambda uid: calls.append(uid)
        )
        bot = AsyncMock()
        sess = _make_sess()
        ok = await restore_card(bot, 1, sess, 555)
        assert ok is False
        assert (1, sess.id) not in _cards
        assert calls == [1]

    async def test_skips_when_live_card_already_raced(self, monkeypatch) -> None:
        edit = AsyncMock(return_value=True)
        monkeypatch.setattr(notifications, "_edit_card", edit)
        monkeypatch.setattr(
            notifications, "_ensure_seeded", AsyncMock(return_value=None)
        )
        sess = _make_sess()
        # A claude event already established a live card on a different msg.
        _cards[(1, sess.id)] = CardState(msg_id=12345)
        bot = AsyncMock()
        ok = await restore_card(bot, 1, sess, 555)
        assert ok is True
        # Untouched: no edit, msg_id stays the event's.
        edit.assert_not_called()
        assert _cards[(1, sess.id)].msg_id == 12345
