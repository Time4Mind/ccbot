from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_FT_MORE, CB_MM_BACK


@pytest.mark.asyncio
async def test_menu_tap_paints_cached_status_and_starts_refresh_immediately() -> None:
    from ccbot.bot.callbacks import footer, more_menu
    from ccbot.codex_usage import CodexRateLimitWindow, CodexUsageInfo

    cached = CodexUsageInfo(
        weekly=CodexRateLimitWindow(
            used_percent=3, duration_minutes=10_080, resets_at=None
        )
    )
    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()

    async def slow_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()
        return cached

    query = MagicMock(data=CB_FT_MORE)
    query.answer = AsyncMock()
    context = SimpleNamespace(bot=MagicMock())
    user = SimpleNamespace(id=71)
    paints: list[str] = []

    async def capture_view(_query, _bot, _uid, text, _keyboard) -> None:
        paints.append(text)

    with (
        patch.object(footer, "set_view", new=AsyncMock(side_effect=capture_view)),
        patch.object(footer.session_manager, "get_active_session", return_value=None),
        patch.object(
            more_menu.session_manager,
            "get_user_settings",
            return_value={"language": "ru"},
        ),
        patch.object(more_menu, "safe_edit", new=AsyncMock()),
        patch.object(more_menu, "get_cached_live_usage", return_value=cached),
        patch.object(more_menu, "get_cached_live_usage_age_seconds", return_value=185),
        patch.object(more_menu, "fetch_live_usage", side_effect=slow_refresh),
    ):
        assert await footer.handle(query, context, user) is True
        await refresh_started.wait()
        assert "Статус · 3м ⏳" in paints[0]
        assert "3%" in paints[0]
        release_refresh.set()
        task = more_menu._menu_refresh_tasks.get(user.id)
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_reopening_menu_reuses_inflight_refresh() -> None:
    from ccbot.bot.callbacks import footer, more_menu

    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    calls = 0

    async def slow_refresh() -> None:
        nonlocal calls
        calls += 1
        refresh_started.set()
        await release_refresh.wait()
        return None

    query = MagicMock(data=CB_FT_MORE)
    query.answer = AsyncMock()
    context = SimpleNamespace(bot=MagicMock())
    user = SimpleNamespace(id=72)

    with (
        patch.object(footer, "set_view", new=AsyncMock()),
        patch.object(footer.session_manager, "get_active_session", return_value=None),
        patch.object(more_menu, "get_cached_live_usage", return_value=None),
        patch.object(more_menu, "fetch_live_usage", side_effect=slow_refresh),
    ):
        assert await footer.handle(query, context, user) is True
        await refresh_started.wait()
        assert await footer.handle(query, context, user) is True
        assert calls == 1
        release_refresh.set()
        task = more_menu._menu_refresh_tasks.get(user.id)
        if task is not None:
            await task


@pytest.mark.asyncio
async def test_leaving_menu_does_not_cancel_refresh_or_repaint_next_screen() -> None:
    from ccbot.bot.callbacks import footer, more_menu

    refresh_started = asyncio.Event()
    release_refresh = asyncio.Event()
    completed = asyncio.Event()
    background_edits = AsyncMock()

    async def slow_refresh() -> None:
        refresh_started.set()
        await release_refresh.wait()
        completed.set()
        return None

    menu_query = MagicMock(data=CB_FT_MORE)
    menu_query.answer = AsyncMock()
    context = SimpleNamespace(bot=MagicMock())
    user = SimpleNamespace(id=73)

    with (
        patch.object(footer, "set_view", new=AsyncMock()),
        patch.object(footer.session_manager, "get_active_session", return_value=None),
        patch.object(more_menu, "safe_edit", new=background_edits),
        patch.object(more_menu, "set_view", new=AsyncMock()),
        patch.object(more_menu, "get_cached_live_usage", return_value=None),
        patch.object(more_menu, "fetch_live_usage", side_effect=slow_refresh),
    ):
        assert await footer.handle(menu_query, context, user) is True
        await refresh_started.wait()

        submenu_query = MagicMock(data=CB_MM_BACK)
        submenu_query.answer = AsyncMock()
        assert await more_menu.handle(submenu_query, context, user) is True

        release_refresh.set()
        task = more_menu._menu_refresh_tasks.get(user.id)
        if task is not None:
            await task

    assert completed.is_set()
    background_edits.assert_not_awaited()


@pytest.mark.asyncio
async def test_finished_menu_refresh_removes_hourglass_and_sets_age_zero() -> None:
    from ccbot.bot.callbacks import footer, more_menu
    from ccbot.codex_usage import CodexRateLimitWindow, CodexUsageInfo

    fresh = CodexUsageInfo(
        weekly=CodexRateLimitWindow(
            used_percent=4, duration_minutes=10_080, resets_at=None
        )
    )
    query = MagicMock(data=CB_FT_MORE)
    query.answer = AsyncMock()
    context = SimpleNamespace(bot=MagicMock())
    user = SimpleNamespace(id=74)
    background_edits: list[str] = []

    async def capture_edit(_target, text: str, **_kwargs) -> None:
        background_edits.append(text)

    with (
        patch.object(footer, "set_view", new=AsyncMock()),
        patch.object(footer.session_manager, "get_active_session", return_value=None),
        patch.object(
            more_menu.session_manager,
            "get_user_settings",
            return_value={"language": "ru"},
        ),
        patch.object(more_menu, "safe_edit", new=AsyncMock(side_effect=capture_edit)),
        patch.object(more_menu, "get_cached_live_usage", return_value=fresh),
        patch.object(more_menu, "get_cached_live_usage_age_seconds", return_value=0),
        patch.object(more_menu, "fetch_live_usage", new=AsyncMock(return_value=fresh)),
    ):
        assert await footer.handle(query, context, user) is True
        await asyncio.sleep(0)
        task = more_menu._menu_refresh_tasks.get(user.id)
        if task is not None:
            await task

    assert len(background_edits) == 1
    assert "Статус · 0м" in background_edits[0]
    assert "⏳" not in background_edits[0]
    assert "4%" in background_edits[0]
