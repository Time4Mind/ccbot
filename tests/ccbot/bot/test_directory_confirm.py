"""Selecting a directory starts fresh without old-session recommendations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import dir_browser
from ccbot.bot import _messages_text
from ccbot.handlers.callback_data import CB_DIR_CONFIRM
from ccbot.handlers.callback_data import CB_DIR_CANCEL
from ccbot.handlers.callback_data import CB_DIR_CREATE
from ccbot.handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_NODE_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    STATE_KEY,
    STATE_NAMING_DIRECTORY,
    build_directory_browser,
)
from ccbot.i18n import t


@pytest.mark.asyncio
async def test_confirm_starts_fresh_without_listing_sessions(
    monkeypatch, tmp_path
) -> None:
    query = SimpleNamespace(data=CB_DIR_CONFIRM, answer=AsyncMock())
    context = SimpleNamespace(user_data={BROWSE_PATH_KEY: str(tmp_path)})
    user = SimpleNamespace(id=42)
    create = AsyncMock()
    monkeypatch.setattr(dir_browser, "create_and_activate_session", create)

    assert await dir_browser.handle(query, context, user)
    create.assert_awaited_once_with(query, context, user, str(tmp_path))


@pytest.mark.asyncio
async def test_directory_browser_reads_selected_node_filesystem(monkeypatch):
    runtime = SimpleNamespace(
        list_directories=AsyncMock(
            return_value={
                "ok": True,
                "path": "/worker/home",
                "directories": ["project", "notes"],
            }
        )
    )
    monkeypatch.setattr(
        dir_browser,
        "session_manager",
        SimpleNamespace(get_selected_node_id=lambda _uid: "worker-a"),
    )
    monkeypatch.setattr(dir_browser, "get_node_runtime", lambda _node_id: runtime)
    edit = AsyncMock()
    monkeypatch.setattr(dir_browser, "safe_edit", edit)

    context = SimpleNamespace(user_data={})
    await dir_browser.open_directory_browser(SimpleNamespace(), context, user_id=42)

    runtime.list_directories.assert_awaited_once_with("worker-a", "")
    assert context.user_data[BROWSE_NODE_KEY] == "worker-a"
    assert context.user_data[BROWSE_PATH_KEY] == "/worker/home"
    assert context.user_data[BROWSE_DIRS_KEY] == ["project", "notes"]
    assert "~" not in edit.await_args.args[1]


@pytest.mark.asyncio
async def test_remote_directory_confirm_passes_target_node(monkeypatch, tmp_path):
    order = []
    query = SimpleNamespace(
        data=CB_DIR_CONFIRM,
        answer=AsyncMock(side_effect=lambda: order.append("answer")),
    )
    context = SimpleNamespace(
        user_data={BROWSE_PATH_KEY: "/worker/home", BROWSE_NODE_KEY: "worker-a"}
    )
    user = SimpleNamespace(id=42)
    create = AsyncMock(side_effect=lambda *_args, **_kwargs: order.append("create"))
    monkeypatch.setattr(dir_browser, "create_and_activate_session", create)

    assert await dir_browser.handle(query, context, user)
    create.assert_awaited_once_with(
        query, context, user, "/worker/home", node_id="worker-a"
    )
    assert order == ["answer", "create"]


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
async def test_directory_browser_exit_is_back_to_active_card(tmp_path) -> None:
    _text, keyboard, _subdirs = await build_directory_browser(str(tmp_path), user_id=42)

    exit_button = next(
        button
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data == CB_DIR_CANCEL
    )
    assert exit_button.text == t(42, "btn.back")
    assert keyboard.inline_keyboard[-1] == (exit_button,)
    assert any(
        button.callback_data == CB_DIR_CREATE for button in keyboard.inline_keyboard[-2]
    )


@pytest.mark.asyncio
async def test_cancel_new_session_flow_restores_active_card(monkeypatch) -> None:
    query = SimpleNamespace(data=CB_DIR_CANCEL, answer=AsyncMock())
    context = SimpleNamespace(user_data={}, bot=object())
    user = SimpleNamespace(id=42)
    active = SimpleNamespace(id="active-session")
    resume = AsyncMock()
    open_menu = AsyncMock()

    monkeypatch.setattr(
        dir_browser,
        "session_manager",
        SimpleNamespace(get_active_session=lambda _uid: active),
        raising=False,
    )
    monkeypatch.setattr(dir_browser, "resume_card_view", resume, raising=False)
    monkeypatch.setattr(dir_browser, "open_more_in_place", open_menu)
    monkeypatch.setattr("ccbot.startup_queue.cancel_startup_queue", lambda _uid: 0)

    assert await dir_browser.handle(query, context, user)
    resume.assert_awaited_once_with(context.bot, user.id, active)
    open_menu.assert_not_awaited()


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
