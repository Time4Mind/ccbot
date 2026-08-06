from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.callbacks import settings as settings_callback
from ccbot.handlers.archive import idle_archive_sweep
from ccbot.handlers.menu import build_footer_keyboard
from ccbot.session import session_manager


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
