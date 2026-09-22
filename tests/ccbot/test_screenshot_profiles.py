from __future__ import annotations

import io

import pytest
from PIL import Image

from ccbot.screenshot import text_to_image


@pytest.mark.asyncio
async def test_screenshot_profiles_have_deterministic_scale_and_palette() -> None:
    text = "\x1b[31mred\x1b[0m \x1b[32mgreen\x1b[0m \x1b[34mblue\x1b[0m plain text"

    full8 = Image.open(io.BytesIO(await text_to_image(text, profile="full8")))
    compact8 = Image.open(io.BytesIO(await text_to_image(text, profile="compact8")))
    fullcolor = Image.open(io.BytesIO(await text_to_image(text, profile="fullcolor")))

    assert compact8.width == round(full8.width * 0.75)
    assert compact8.height == round(full8.height * 0.75)
    assert len(full8.convert("RGB").getcolors(maxcolors=256) or []) <= 32
    assert len(compact8.convert("RGB").getcolors(maxcolors=256) or []) <= 32
    assert full8.mode == "P"
    assert compact8.mode == "P"
    assert fullcolor.size == full8.size


@pytest.mark.asyncio
async def test_optimized_profile_preserves_text_antialiasing_grays() -> None:
    text = "\x1b[31mred\x1b[0m \x1b[32mgreen\x1b[0m \x1b[34mblue\x1b[0m plain text"
    rendered = Image.open(io.BytesIO(await text_to_image(text, profile="full8")))
    colors = {rgb for _count, rgb in rendered.convert("RGB").getcolors(256) or []}

    neutral_shades = {rgb for rgb in colors if rgb[0] == rgb[1] == rgb[2]}
    assert len(colors) <= 32
    assert len(neutral_shades) >= 3


@pytest.mark.asyncio
async def test_screenshot_profile_output_is_deterministic() -> None:
    first = await text_to_image("same pane", profile="full8")
    second = await text_to_image("same pane", profile="full8")

    assert first == second


@pytest.mark.asyncio
async def test_repeated_screenshot_reuses_rendered_terminal_rows() -> None:
    from ccbot import screenshot

    screenshot._render_line_cached.cache_clear()

    await text_to_image("static first row\nstatic second row", profile="full8")
    cold = screenshot._render_line_cached.cache_info()
    await text_to_image("static first row\nstatic second row", profile="full8")
    warm = screenshot._render_line_cached.cache_info()

    assert cold.misses == 2
    assert warm.misses == cold.misses
    assert warm.hits >= cold.hits + 2


@pytest.mark.asyncio
async def test_pane_capture_uses_user_limit_profile_and_complete_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
        "capture_panes",
        AsyncMock(return_value={"@1": source}),
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
    tmux_module.tmux_manager.capture_panes.assert_awaited_once_with(
        ["@1"], with_ansi=True
    )


@pytest.mark.asyncio
async def test_remote_screenshot_uses_worker_ansi_capture_not_local_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ccbot import screenshot as screenshot_module
    from ccbot import terminal_runtime
    from ccbot.handlers import kb_mode
    from ccbot.session import session_manager
    from ccbot.session_models import Session

    sess = Session(
        id="remote",
        name="Remote",
        node_id="worker-a",
        window_id="worker-a::@17",
        worker_session_id="worker-session",
    )
    runtime = SimpleNamespace(
        capture_session=AsyncMock(return_value={"ok": True, "pane": "remote pane"})
    )
    local_capture = AsyncMock()
    monkeypatch.setattr(session_manager, "find_session_by_window", lambda _wid: sess)
    monkeypatch.setattr(session_manager, "get_user_settings", lambda _uid: {})
    monkeypatch.setattr(terminal_runtime, "get_node_runtime", lambda _nid: runtime)
    monkeypatch.setattr(terminal_runtime.tmux_manager, "capture_panes", local_capture)
    monkeypatch.setattr(
        screenshot_module, "text_to_image", AsyncMock(return_value=b"remote-png")
    )

    png, digest = await kb_mode._capture_pane_png(sess.window_id, user_id=42)

    assert png == b"remote-png"
    assert digest
    runtime.capture_session.assert_awaited_once_with(
        "worker-a", "worker-session", with_ansi=True
    )
    local_capture.assert_not_awaited()
