from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.handlers.callback_data import CB_MM_STATUS


@pytest.mark.asyncio
async def test_codex_usage_failure_starts_auth_only_on_explicit_status_request() -> (
    None
):
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
        patch.object(more_menu, "fetch_live_usage", new=AsyncMock(return_value=None)),
        patch.object(more_menu.session_manager, "agent_backend", "codex"),
        patch(
            "ccbot.bot.commands.auth.ensure_codex_authenticated",
            new=AsyncMock(return_value=False),
        ) as ensure_auth,
        patch.object(
            more_menu,
            "t",
            side_effect=lambda _user_id, key, **_kwargs: key,
        ),
    ):
        assert await more_menu.handle(query, context, user) is True

    ensure_auth.assert_awaited_once_with(context.bot, 7)
    assert edits[-1] == "usage.auth_required"
