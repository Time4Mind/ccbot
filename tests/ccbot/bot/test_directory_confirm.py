"""Selecting a directory starts fresh without old-session recommendations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import dir_browser
from ccbot.bot import _messages_text
from ccbot.handlers.callback_data import CB_DIR_CONFIRM
from ccbot.handlers.callback_data import CB_DIR_CREATE
from ccbot.handlers.directory_browser import (
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
)


@pytest.mark.asyncio
async def test_confirm_starts_fresh_without_listing_sessions(
    monkeypatch, tmp_path
) -> None:
    query = SimpleNamespace(data=CB_DIR_CONFIRM)
    context = SimpleNamespace(user_data={BROWSE_PATH_KEY: str(tmp_path)})
    user = SimpleNamespace(id=42)
    create = AsyncMock()
    monkeypatch.setattr(dir_browser, "create_and_activate_session", create)

    assert await dir_browser.handle(query, context, user)
    create.assert_awaited_once_with(query, context, user, str(tmp_path))


@pytest.mark.asyncio
async def test_create_folder_prompt_has_back_to_current_page(
    monkeypatch, tmp_path
) -> None:
    query = SimpleNamespace(data=CB_DIR_CREATE, answer=AsyncMock())
    context = SimpleNamespace(
        user_data={BROWSE_PATH_KEY: str(tmp_path), BROWSE_PAGE_KEY: 2}
    )
    user = SimpleNamespace(id=42)
    edit = AsyncMock()
    monkeypatch.setattr(dir_browser, "safe_edit", edit)

    assert await dir_browser.handle(query, context, user)
    assert context.user_data[STATE_KEY] == STATE_NAMING_DIRECTORY
    keyboard = edit.await_args.kwargs["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == "db:page:2"


@pytest.mark.asyncio
async def test_folder_name_creates_and_opens_child(monkeypatch, tmp_path) -> None:
    message = SimpleNamespace(text="new-child")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=message,
    )
    context = SimpleNamespace(
        user_data={
            STATE_KEY: STATE_NAMING_DIRECTORY,
            BROWSE_PATH_KEY: str(tmp_path),
        },
        bot=SimpleNamespace(),
    )
    reply = AsyncMock()
    monkeypatch.setattr(_messages_text, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(_messages_text, "active_window", lambda _uid: "@active")
    prior_voice = AsyncMock(side_effect=AssertionError("old session must be bypassed"))
    monkeypatch.setattr(
        _messages_text, "_await_prior_voice", prior_voice, raising=False
    )
    monkeypatch.setattr(
        _messages_text, "maybe_consume_code", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(_messages_text, "safe_reply", reply)

    assert await _messages_text.text_handler(update, context)
    assert (tmp_path / "new-child").is_dir()
    assert context.user_data[STATE_KEY] == "browsing_directory"
    assert context.user_data[BROWSE_PATH_KEY] == str(tmp_path / "new-child")
    assert "Folder created" in reply.await_args.args[1]
    assert f"`{tmp_path / 'new-child'}`" in reply.await_args.args[1]
    assert reply.await_args.kwargs["reply_markup"] is not None
    prior_voice.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_folder_opens_it_in_browser(monkeypatch, tmp_path) -> None:
    child = tmp_path / "existing"
    child.mkdir()
    message = SimpleNamespace(text="existing")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        message=message,
    )
    context = SimpleNamespace(
        user_data={
            STATE_KEY: STATE_NAMING_DIRECTORY,
            BROWSE_PATH_KEY: str(tmp_path),
        },
        bot=SimpleNamespace(),
    )
    reply = AsyncMock()
    monkeypatch.setattr(_messages_text, "is_user_allowed", lambda _uid: True)
    monkeypatch.setattr(_messages_text, "active_window", lambda _uid: None)
    monkeypatch.setattr(
        _messages_text, "maybe_consume_code", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(_messages_text, "safe_reply", reply)

    assert await _messages_text.text_handler(update, context)
    assert context.user_data[STATE_KEY] == "browsing_directory"
    assert context.user_data[BROWSE_PATH_KEY] == str(child)
    assert "already exists" in reply.await_args.args[1]
    assert f"`{child}`" in reply.await_args.args[1]
    assert reply.await_args.kwargs["reply_markup"] is not None
