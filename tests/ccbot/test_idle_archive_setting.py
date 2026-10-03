from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.callbacks import settings as settings_callback
from ccbot.handlers.archive import idle_archive_sweep
from ccbot.handlers.menu import build_footer_keyboard
from ccbot.session import session_manager
from ccbot.session_models import Session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expire_active,keep_other,view,has_context",
    [
        (True, True, "sessions", True),
        (False, True, "sessions", True),
        (True, False, "sessions", True),
        (True, True, "sessions", False),
        (True, True, "menu", True),
        (True, True, "picker", True),
    ],
)
async def test_ttl_refreshes_only_the_visible_sessions_keyboard(
    monkeypatch, expire_active, keep_other, view, has_context
) -> None:
    from ccbot.handlers import archive, card_surface, menu, switcher
    from ccbot.handlers.card_types import CardState
    from ccbot.session import SessionManager
    from ccbot.session_models import Session

    manager = SessionManager()
    monkeypatch.setattr(manager, "save_state", lambda: None)
    expired = Session(
        id="expired",
        name="expired",
        window_id="@1",
        last_event_at=time.time() - 7 * 3600,
    )
    keeper = Session(
        id="keeper", name="keeper", window_id="@2", last_event_at=time.time()
    )
    manager.sessions = {expired.id: expired}
    if keep_other:
        manager.sessions[keeper.id] = keeper
    visible_id = expired.id if expire_active else keeper.id
    manager.active_sessions[42] = visible_id
    manager.active_history[42] = [keeper.id] if expire_active and keep_other else []
    manager.last_switcher_msg_id[42] = 100
    manager.user_settings[42] = {"session_idle_hours": 6}
    state = CardState(
        msg_id=100, in_menu_view=view == "menu", in_kb_mode=view == "picker"
    )
    for module in (archive, card_surface, menu, switcher):
        monkeypatch.setattr(module, "session_manager", manager)
    monkeypatch.setattr(archive, "teardown_session_runtime", AsyncMock())
    monkeypatch.setattr(
        archive, "_archive_context_status", AsyncMock(return_value=has_context)
    )
    bot = AsyncMock()

    with patch.dict(card_surface._cards, {(42, visible_id): state}, clear=True):
        await archive.idle_archive_sweep(bot, 42)

    if view != "sessions":
        bot.edit_message_reply_markup.assert_not_awaited()
        return
    assert bot.edit_message_reply_markup.await_args is not None
    kwargs = bot.edit_message_reply_markup.await_args.kwargs
    assert kwargs["message_id"] == 100
    callbacks = {
        button.callback_data
        for row in kwargs["reply_markup"].inline_keyboard
        for button in row
    }
    assert "sw:expired" not in callbacks
    assert ("sw:keeper" in callbacks) == keep_other
    bot.edit_message_text.assert_not_awaited()


def test_idle_archive_settings_screen_has_supported_choices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _user_id: {"language": "ru", "session_idle_hours": 12},
    )

    keyboard = build_footer_keyboard(42, screen="settings_idle_archive")

    assert keyboard is not None
    choices = keyboard.inline_keyboard[0]
    assert [button.callback_data for button in choices] == [
        "st:idle:6",
        "st:idle:12",
        "st:idle:24",
    ]
    assert choices[1].text.startswith("• ")

    category = build_footer_keyboard(42, screen="settings_cat_behavior")
    assert category is not None
    callbacks = {
        button.callback_data for row in category.inline_keyboard for button in row
    }
    assert "st:grp:session_idle_hours" in callbacks


@pytest.mark.asyncio
async def test_idle_archive_callback_persists_selected_hours() -> None:
    query = MagicMock(data="st:idle:24", message=None)
    query.answer = AsyncMock()
    context = MagicMock()
    user = SimpleNamespace(id=42)

    with (
        patch.object(
            settings_callback.session_manager, "update_user_setting"
        ) as update,
        patch.object(settings_callback, "safe_edit", new=AsyncMock()),
        patch.object(
            settings_callback,
            "render_settings_group_text",
            return_value="settings",
        ),
        patch.object(settings_callback, "build_footer_keyboard", return_value=None),
    ):
        handled = await settings_callback.handle(query, context, user)

    assert handled is True
    update.assert_called_once_with(42, "session_idle_hours", 24)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("callback", "setting"),
    [
        ("st:spl:command:5", "spoiler_command_lines"),
        ("st:spl:result:10", "spoiler_result_lines"),
        ("st:spl:command:30", "spoiler_command_lines"),
        ("st:spl:result:60", "spoiler_result_lines"),
    ],
)
async def test_spoiler_line_callback_persists_selected_limit(
    callback: str, setting: str
) -> None:
    query = MagicMock(data=callback, message=None)
    query.answer = AsyncMock()
    context = MagicMock()
    user = SimpleNamespace(id=42)

    with (
        patch.object(
            settings_callback.session_manager, "update_user_setting"
        ) as update,
        patch.object(settings_callback, "safe_edit", new=AsyncMock()),
        patch.object(
            settings_callback,
            "render_settings_group_text",
            return_value="settings",
        ),
        patch.object(settings_callback, "build_footer_keyboard", return_value=None),
    ):
        handled = await settings_callback.handle(query, context, user)

    assert handled is True
    update.assert_called_once_with(42, setting, int(callback.rsplit(":", 1)[1]))


def test_spoiler_line_settings_offer_five_ten_thirty_and_sixty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _user_id: {
            "language": "ru",
            "spoiler_command_lines": 10,
            "spoiler_result_lines": 60,
        },
    )

    command = build_footer_keyboard(42, screen="settings_spoiler_command_lines")
    result = build_footer_keyboard(42, screen="settings_spoiler_result_lines")

    assert command is not None
    assert result is not None
    assert [button.callback_data for button in command.inline_keyboard[0]] == [
        "st:spl:command:5",
        "st:spl:command:10",
        "st:spl:command:30",
        "st:spl:command:60",
    ]
    assert [button.callback_data for button in result.inline_keyboard[0]] == [
        "st:spl:result:5",
        "st:spl:result:10",
        "st:spl:result:30",
        "st:spl:result:60",
    ]


@pytest.mark.asyncio
async def test_idle_archive_sweep_uses_user_setting() -> None:
    with (
        patch.object(
            session_manager,
            "get_user_settings",
            return_value={"session_idle_hours": 12},
        ),
        patch.object(
            session_manager, "find_idle_to_archive", return_value=[]
        ) as find_idle,
    ):
        archived = await idle_archive_sweep(MagicMock(), 42)

    assert archived == 0
    find_idle.assert_called_once_with(12 * 3600.0)


@pytest.mark.asyncio
async def test_idle_archive_cancels_startup_watcher() -> None:
    sess = Session(
        id="deadbeef",
        name="expired",
        window_id="@9",
        last_event_at=time.time() - 13 * 3600,
    )
    teardown = AsyncMock()
    with (
        patch.object(
            session_manager,
            "get_user_settings",
            return_value={"session_idle_hours": 12},
        ),
        patch.object(session_manager, "find_idle_to_archive", return_value=[sess]),
        patch("ccbot.handlers.archive.teardown_session_runtime", new=teardown),
        patch(
            "ccbot.handlers.archive._archive_context_status",
            new=AsyncMock(return_value=True),
        ),
        patch.object(session_manager, "mark_session_archived"),
    ):
        archived = await idle_archive_sweep(MagicMock(), 42)

    assert archived == 1
    teardown.assert_awaited_once()
    assert teardown.await_args.args[1] is sess


@pytest.mark.asyncio
async def test_idle_archive_deletes_proven_empty_session() -> None:
    sess = Session(
        id="empty", name="empty", window_id="@9", last_event_at=time.time() - 13 * 3600
    )
    with (
        patch.object(
            session_manager,
            "get_user_settings",
            return_value={"session_idle_hours": 12},
        ),
        patch.object(session_manager, "find_idle_to_archive", return_value=[sess]),
        patch("ccbot.handlers.archive.teardown_session_runtime", new=AsyncMock()),
        patch.object(session_manager, "delete_session", return_value=True) as delete,
        patch.object(session_manager, "mark_session_archived") as archive,
    ):
        archived = await idle_archive_sweep(MagicMock(), 42)

    assert archived == 0
    delete.assert_called_once_with("empty")
    archive.assert_not_called()
