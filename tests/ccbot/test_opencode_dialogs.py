"""Only the model-variant picker may be completed without a user answer."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.opencode_dialogs import settle_variant

VARIANT = """          Select variant                     esc
              Search
              Default
              low
              medium
              high
  ┃
  ┃  Build · GPT-6.1 Sol OpenAI
  /tmp/lab 14K (1%) ctrl+p commands
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered", [False, True])
async def test_variant_completes_at_default_or_cancels_a_filtered_picker(
    monkeypatch, filtered
):
    state = {"selected": "high", "closed": False}

    async def key(sess, value):
        if value == "Home":
            state["selected"] = "default"
        if value in ("Enter", "Escape"):
            state["closed"] = True
        return True

    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", key)
    pane = VARIANT.replace("Default", "unmatched input") if filtered else VARIANT
    monkeypatch.setattr(
        "ccbot.terminal_runtime.capture_session_pane", AsyncMock(return_value=pane)
    )
    assert await settle_variant(
        SimpleNamespace(
            backend="opencode", node_id="local", worker_session_id="", window_id="@5"
        ),
        pane,
    )
    assert state == {"selected": "high" if filtered else "default", "closed": True}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pane,backend",
    [
        (VARIANT, "codex"),
        (VARIANT.replace("Select variant", "Select model"), "opencode"),
        ("┃ △ Permission required\n┃ Allow once Allow always Reject\n┃", "opencode"),
    ],
)
async def test_other_dialogs_are_never_approved(monkeypatch, pane, backend):
    key = AsyncMock()
    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", key)
    assert not await settle_variant(SimpleNamespace(backend=backend), pane)
    key.assert_not_awaited()


@pytest.mark.asyncio
async def test_queued_input_resumes_after_variant_without_showing_keyboard(monkeypatch):
    import asyncio
    from ccbot.bot import messages

    state = {"closed": False}
    sess = SimpleNamespace(
        id="s1",
        backend="opencode",
        node_id="local",
        window_id="@5",
        worker_session_id="",
    )

    async def key(_sess, value):
        if value == "Enter":
            state["closed"] = True
        return True

    manager = SimpleNamespace(
        find_session_by_window=lambda wid: sess, get_active_session=lambda uid: sess
    )
    tmux = SimpleNamespace(
        find_window_by_id=AsyncMock(return_value=SimpleNamespace(window_id="@5")),
        capture_pane=AsyncMock(
            side_effect=lambda *a, **kw: "idle" if state["closed"] else VARIANT
        ),
    )
    keyboard = AsyncMock()
    reply = AsyncMock()
    monkeypatch.setattr(messages, "session_manager", manager)
    monkeypatch.setattr(messages, "tmux_manager", tmux)
    monkeypatch.setattr(messages, "enter_kb_mode", keyboard)
    monkeypatch.setattr(messages, "safe_reply", reply)
    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", key)
    monkeypatch.setattr(
        "ccbot.terminal_runtime.capture_session_pane",
        AsyncMock(side_effect=lambda *a, **kw: "idle" if state["closed"] else VARIANT),
    )
    assert not await asyncio.wait_for(
        messages._intercept_if_pending_ui(
            AsyncMock(), 42, "@5", SimpleNamespace(), wait_until_clear=True
        ),
        0.15,
    )
    assert state["closed"]
    keyboard.assert_not_awaited()
    reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_two_observers_do_not_press_enter_after_composer_resumed(monkeypatch):
    import asyncio

    state = {"closed": False, "submissions": 0}
    sess = SimpleNamespace(
        backend="opencode", node_id="local", worker_session_id="", window_id="@same"
    )

    async def capture(_sess):
        return "idle" if state["closed"] else VARIANT

    async def key(_sess, value):
        if value == "Home":
            await asyncio.sleep(0)
        if value == "Enter":
            if state["closed"]:
                state["submissions"] += 1
            state["closed"] = True
        return True

    monkeypatch.setattr("ccbot.terminal_runtime.capture_session_pane", capture)
    monkeypatch.setattr("ccbot.terminal_runtime.send_session_key", key)
    assert all(
        await asyncio.gather(
            settle_variant(sess, VARIANT), settle_variant(sess, VARIANT)
        )
    )
    assert state == {"closed": True, "submissions": 0}
