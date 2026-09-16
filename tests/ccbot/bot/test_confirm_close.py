from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import confirm
from ccbot.handlers.callback_data import CB_CONF_KILL_YES


@pytest.mark.asyncio
async def test_close_returns_to_sessions_not_menu(monkeypatch) -> None:
    sess = SimpleNamespace(id="closing", state="idle")
    query = SimpleNamespace(
        data=f"{CB_CONF_KILL_YES}{sess.id}",
        answer=AsyncMock(),
    )
    context = SimpleNamespace(bot=object())
    user = SimpleNamespace(id=42)
    archive = AsyncMock()
    open_sessions = AsyncMock()
    open_menu = AsyncMock()

    monkeypatch.setattr(
        confirm,
        "session_manager",
        SimpleNamespace(get_session=lambda _sid: sess),
    )
    monkeypatch.setattr(confirm, "archive_session", archive)
    monkeypatch.setattr(confirm, "open_sessions_in_place", open_sessions, raising=False)
    monkeypatch.setattr(confirm, "open_more_in_place", open_menu)

    assert await confirm.handle(query, context, user)
    archive.assert_awaited_once_with(user.id, context.bot, sess)
    open_sessions.assert_awaited_once_with(query, context.bot, user.id)
    open_menu.assert_not_awaited()
