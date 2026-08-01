from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_MM_STATUS


@pytest.mark.asyncio
async def test_status_shows_cached_value_before_refresh() -> None:
    from ccbot.bot.callbacks import more_menu
    from ccbot.codex_usage import CodexRateLimitWindow, CodexUsageInfo

    cached = CodexUsageInfo(
        weekly=CodexRateLimitWindow(
            used_percent=3, duration_minutes=10_080, resets_at=None
        )
    )
    fresh = CodexUsageInfo(
        weekly=CodexRateLimitWindow(
            used_percent=4, duration_minutes=10_080, resets_at=None
        )
    )
    query = MagicMock(data=CB_MM_STATUS)
    query.answer = AsyncMock()
    context = MagicMock(bot=MagicMock())
    user = MagicMock(id=7)
    edits: list[str] = []

    async def fake_edit(_query: object, text: str, **_kwargs: object) -> None:
        edits.append(text)

    with (
        patch.object(more_menu, "safe_edit", new=AsyncMock(side_effect=fake_edit)),
        patch.object(more_menu, "get_cached_live_usage", return_value=cached),
        patch.object(more_menu, "fetch_live_usage", new=AsyncMock(return_value=fresh)),
    ):
        assert await more_menu.handle(query, context, user) is True

    assert len(edits) == 2
    assert "3%" in edits[0]
    assert "4%" in edits[1]
    assert "Загружаю" not in edits[0]


@pytest.mark.asyncio
async def test_codex_usage_failure_does_not_replace_working_auth() -> None:
    from ccbot.bot.callbacks import more_menu

    query = MagicMock()
    query.data = CB_MM_STATUS
    query.answer = AsyncMock()
    context = MagicMock()
    context.bot = MagicMock()
    user = MagicMock(id=7)
    edits: list[str] = []

    async def fake_edit(_query: object, text: str, **_kwargs: object) -> None:
        edits.append(text)

    with (
        patch.object(more_menu, "safe_edit", new=AsyncMock(side_effect=fake_edit)),
        patch.object(more_menu, "get_cached_live_usage", return_value=None),
        patch.object(more_menu, "fetch_live_usage", new=AsyncMock(return_value=None)),
        patch.object(more_menu.session_manager, "agent_backend", "codex"),
        patch.object(
            more_menu,
            "t",
            side_effect=lambda _user_id, key, **_kwargs: key,
        ),
    ):
        assert await more_menu.handle(query, context, user) is True

    assert edits[-1] == "usage.unavailable"
