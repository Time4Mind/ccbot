"""Selecting a directory starts fresh without old-session recommendations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import dir_browser
from ccbot.handlers.callback_data import CB_DIR_CONFIRM
from ccbot.handlers.directory_browser import BROWSE_PATH_KEY


@pytest.mark.asyncio
async def test_confirm_starts_fresh_without_listing_sessions(monkeypatch, tmp_path) -> None:
    query = SimpleNamespace(data=CB_DIR_CONFIRM)
    context = SimpleNamespace(user_data={BROWSE_PATH_KEY: str(tmp_path)})
    user = SimpleNamespace(id=42)
    create = AsyncMock()
    monkeypatch.setattr(dir_browser, "create_and_activate_session", create)

    assert await dir_browser.handle(query, context, user)
    create.assert_awaited_once_with(query, context, user, str(tmp_path))
