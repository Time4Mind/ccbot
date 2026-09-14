from __future__ import annotations

import io

import pytest
from PIL import Image

from ccbot.screenshot import text_to_image


@pytest.mark.asyncio
async def test_screenshot_profiles_have_deterministic_scale_and_palette() -> None:
    text = "\x1b[31mred\x1b[0m \x1b[32mgreen\x1b[0m \x1b[34mblue\x1b[0m"

    full8 = Image.open(io.BytesIO(await text_to_image(text, profile="full8")))
    compact8 = Image.open(io.BytesIO(await text_to_image(text, profile="compact8")))
    fullcolor = Image.open(io.BytesIO(await text_to_image(text, profile="fullcolor")))

    assert compact8.width == round(full8.width * 0.75)
    assert compact8.height == round(full8.height * 0.75)
    assert len(full8.convert("RGB").getcolors(maxcolors=256) or []) <= 8
    assert len(compact8.convert("RGB").getcolors(maxcolors=256) or []) <= 8
    assert fullcolor.size == full8.size


@pytest.mark.asyncio
async def test_screenshot_profile_output_is_deterministic() -> None:
    first = await text_to_image("same pane", profile="full8")
    second = await text_to_image("same pane", profile="full8")

    assert first == second


@pytest.mark.asyncio
async def test_pane_capture_uses_user_limit_profile_and_complete_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ccbot.handlers import kb_mode

    source = "discard this partial row\n" + "я" * 30_000 + "\nlatest"
    rendered: list[tuple[str, str]] = []

    async def render(text: str, **kwargs: object) -> bytes:
        rendered.append((text, str(kwargs["profile"])))
        return b"png"

    from ccbot import tmux_manager as tmux_module
    from ccbot import screenshot as screenshot_module
    from ccbot.session import session_manager

    monkeypatch.setattr(
        tmux_module.tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@1")),
    )
    monkeypatch.setattr(
        tmux_module.tmux_manager,
        "capture_pane",
        AsyncMock(return_value=source),
    )
    monkeypatch.setattr(screenshot_module, "text_to_image", render)
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "screenshot_capture_kib": 48,
            "screenshot_profile": "compact8",
        },
    )

    png, digest = await kb_mode._capture_pane_png("@1", user_id=42)

    assert png == b"png"
    assert digest
    assert rendered[0][1] == "compact8"
    assert len(rendered[0][0].encode("utf-8")) <= 48 * 1024
    assert rendered[0][0].endswith("latest")
    assert not rendered[0][0].startswith("discard this partial row")
