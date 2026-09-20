from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.bot.callbacks import interactive_ui


@pytest.mark.asyncio
async def test_picker_button_moves_from_live_cursor_and_confirms(monkeypatch) -> None:
    order = []
    prompt = (
        "Select model\n"
        "\n"
        "› 1. Default (recommended)  Opus 4.6\n"
        "  2. Sonnet                 Sonnet 4.6\n"
        "  3. Haiku                  Haiku 4.5\n"
        "\n"
        "Enter to confirm · Esc to exit"
    )
    window = SimpleNamespace(window_id="@5")
    tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=window),
        capture_pane=AsyncMock(
            side_effect=lambda *_args, **_kwargs: (order.append("capture") or prompt)
        ),
        send_keys=AsyncMock(),
    )
    monkeypatch.setattr(interactive_ui, "tmux_manager", tmux)
    monkeypatch.setattr(interactive_ui.asyncio, "sleep", AsyncMock())
    refresh = AsyncMock()
    monkeypatch.setattr(interactive_ui, "_refresh_after_key", refresh)
    query = SimpleNamespace(
        data="aq:pick:2:@5",
        answer=AsyncMock(side_effect=lambda: order.append("answer")),
    )
    context = SimpleNamespace(bot=SimpleNamespace())
    user = SimpleNamespace(id=42)

    assert await interactive_ui.handle(query, context, user) is True

    assert tmux.send_keys.await_args_list == [
        (("@5", "Down"), {"enter": False, "literal": False}),
        (("@5", "Down"), {"enter": False, "literal": False}),
        (("@5", "Enter"), {"enter": False, "literal": False}),
    ]
    refresh.assert_awaited_once_with(context.bot, 42, "@5")
    query.answer.assert_awaited_once()
    assert order[:2] == ["answer", "capture"]
