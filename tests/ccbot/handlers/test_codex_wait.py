"""Slow-response overlays must not conceal command approvals indefinitely."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ccbot.handlers import notifications, status_polling
from ccbot.session_models import Session
from ccbot.tmux_manager import tmux_manager


def popup(selected=1):
    choices = [
        "Retry with a faster model",
        "Dismiss and keep waiting",
        "Learn more",
    ]
    options = "\n".join(
        f"{'›' if i == selected else ' '} {i}. {label}"
        for i, label in enumerate(choices, 1)
    )
    return (
        "Giving this request a little extra thought\n"
        "If you'd rather not wait, retry with a faster model. It may be less capable\n"
        "of handling complex requests.\n\n"
        f"{options}\n\n"
        "No action is required. Codex will keep waiting, and this menu will close when\n"
        "the response is ready.\n"
    )


APPROVAL = (
    "Would you like to run the following command?\n\n"
    "  $ python audit.py\n\n"
    "› 1. Yes, proceed (y)\n"
    "  2. No, and tell Codex what to do differently (esc)\n\n"
    "Press enter to confirm or esc to cancel\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("auto", ["on", "off"])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("selected,first_key", [(1, "Down"), (3, "Up"), (2, "Enter")])
async def test_poll_dismisses_overlay_then_respects_approval_policy(
    monkeypatch, auto, background, selected, first_key
):
    from ccbot.handlers import bg_status

    sess = Session(
        id=f"wait-{auto}-{selected}-{background}",
        name="audit",
        window_id="@57",
        backend="codex",
    )
    terminal = {"pane": popup(selected), "keys": []}

    async def send(_window, key, **kwargs):
        terminal["keys"].append(key)
        if key in ("Down", "Up"):
            terminal["pane"] = popup(2)
        elif key == "Enter":
            assert terminal["pane"] == popup(2), "never confirm the faster model"
            terminal["pane"] = APPROVAL
        elif key == "y":
            terminal["pane"] = "• Working (0s • esc to interrupt)"
        return True

    monkeypatch.setattr(tmux_manager, "send_keys", send)
    monkeypatch.setattr(
        tmux_manager,
        "find_window_by_id",
        AsyncMock(return_value=SimpleNamespace(window_id="@57")),
    )
    monkeypatch.setattr(
        tmux_manager,
        "capture_panes",
        AsyncMock(side_effect=lambda *_a, **_kw: {"@57": terminal["pane"]}),
    )
    monkeypatch.setattr(
        status_polling.session_manager, "find_session_by_window", lambda _w: sess
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_active_session",
        lambda _u: SimpleNamespace(id="other") if background else sess,
    )
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_user_settings",
        lambda _u: {"auto_approve": auto},
    )
    monkeypatch.setattr(status_polling, "get_interactive_window", lambda _u: None)
    monkeypatch.setattr(status_polling, "_drive_typing_indicator", AsyncMock())
    monkeypatch.setattr(status_polling, "refresh_panel", AsyncMock())
    manual = AsyncMock()
    monkeypatch.setattr(notifications, "enter_kb_mode", manual)

    async def poll():
        await status_polling.update_status_message(
            AsyncMock(),
            42,
            "@57",
            window=SimpleNamespace(window_id="@57"),
            pane_text=terminal["pane"],
        )

    with patch.dict(status_polling._auto_approve_attempts, {}, clear=True):
        await poll()
        assert terminal["keys"] == [first_key]
        if selected != 2:
            await poll()
        assert terminal["pane"] == APPROVAL
        await poll()
    if auto == "on":
        assert terminal["pane"].startswith("• Working")
        assert terminal["keys"][-1] == "y"
        manual.assert_not_awaited()
    else:
        assert terminal["pane"] == APPROVAL
        assert "y" not in terminal["keys"]
        if background:
            assert (
                bg_status.get_pending_interactive_ui(42, sess.id)[1] == "CodexApproval"
            )
        else:
            assert manual.await_args.args[-1] == "CodexApproval"


@pytest.mark.parametrize(
    "suffix",
    [
        "\n› Ask Codex to do anything",
        "\n" + APPROVAL,
        "\nGPT-6.1-Sol high fast · ~/workdir",
    ],
)
def test_historical_popup_is_not_an_active_overlay(suffix):
    from ccbot.codex_wait_prompt import parse_codex_wait_prompt

    assert parse_codex_wait_prompt(popup() + suffix) is None


@pytest.mark.parametrize(
    "pane",
    [
        popup().replace("›", " "),
        popup().replace("2. Dismiss", "› 2. Dismiss"),
        popup().replace("Learn more", "Something else"),
        popup().replace("the response is ready.", ""),
    ],
)
def test_incomplete_or_different_menu_is_not_automated(pane):
    from ccbot.codex_wait_prompt import parse_codex_wait_prompt

    assert parse_codex_wait_prompt(pane) is None


@pytest.mark.asyncio
async def test_stale_capture_cannot_confirm_a_different_menu(monkeypatch):
    from ccbot.handlers import codex_wait

    sess = Session(id="stale", name="audit", window_id="@57", backend="codex")
    send = AsyncMock()
    monkeypatch.setattr(
        codex_wait, "capture_session_pane", AsyncMock(return_value=APPROVAL)
    )
    monkeypatch.setattr(codex_wait, "send_session_key", send)
    assert await codex_wait.dismiss_codex_wait_prompt(sess, popup(2)) is True
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_unresponsive_overlay_surfaces_for_manual_action_without_model_switch(
    monkeypatch,
):
    from ccbot.handlers import codex_wait

    sess = Session(id="wedged", name="audit", window_id="@57", backend="codex")
    pane = "1. Yes, proceed\n" + popup()
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(
        codex_wait, "capture_session_pane", AsyncMock(return_value=pane)
    )
    monkeypatch.setattr(codex_wait, "send_session_key", send)
    for _ in range(3):
        assert await codex_wait.dismiss_codex_wait_prompt(sess, pane) is True
    for _ in range(3):
        assert await codex_wait.dismiss_codex_wait_prompt(sess, pane) is False
    assert [call.args[1] for call in send.await_args_list] == ["Down"] * 3
    content = status_polling.extract_interactive_content(pane)
    assert content.name == "CodexSlowRequest"
    monkeypatch.setattr(
        status_polling.session_manager,
        "get_user_settings",
        lambda _u: {"auto_approve": "on"},
    )
    monkeypatch.setattr(tmux_manager, "send_keys", AsyncMock())
    assert await status_polling._maybe_auto_approve(42, "@57", pane) is False
    tmux_manager.send_keys.assert_not_awaited()
    await codex_wait.dismiss_codex_wait_prompt(sess, "• Working")
    assert await codex_wait.dismiss_codex_wait_prompt(sess, pane) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["claude", "codex"])
async def test_non_overlay_and_other_backend_never_receive_keys(monkeypatch, backend):
    from ccbot.handlers import codex_wait

    sess = Session(id="excluded", name="audit", window_id="@57", backend=backend)
    capture = AsyncMock()
    monkeypatch.setattr(codex_wait, "capture_session_pane", capture)
    for pane in (
        [popup(), APPROVAL]
        if backend == "claude"
        else [APPROVAL, "• Working", "› Ask Codex to do anything"]
    ):
        assert await codex_wait.dismiss_codex_wait_prompt(sess, pane) is False
    assert await codex_wait.dismiss_codex_wait_prompt(None, popup()) is False
    capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_overlay_on_remote_session_uses_owning_node(monkeypatch):
    from ccbot import terminal_runtime
    from ccbot.handlers import codex_wait

    sess = Session(
        id="remote-wait",
        name="audit",
        window_id="remote:@57",
        backend="codex",
        node_id="worker",
        worker_session_id="native-id",
    )
    runtime = SimpleNamespace(
        capture_session=AsyncMock(return_value={"ok": True, "pane": popup(2)}),
        send_key=AsyncMock(return_value={"ok": True}),
    )
    monkeypatch.setattr(
        terminal_runtime,
        "get_node_runtime",
        lambda node: runtime if node == "worker" else None,
    )
    local_send = AsyncMock()
    monkeypatch.setattr(tmux_manager, "send_keys", local_send)
    assert await codex_wait.dismiss_codex_wait_prompt(sess, popup(2)) is True
    runtime.send_key.assert_awaited_once_with("worker", "native-id", "Enter")
    local_send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_transport_does_not_break_other_session_polling(monkeypatch):
    from ccbot.handlers import codex_wait

    sess = Session(
        id="transport-failure", name="audit", window_id="@57", backend="codex"
    )
    monkeypatch.setattr(
        codex_wait, "capture_session_pane", AsyncMock(return_value=popup(2))
    )
    monkeypatch.setattr(
        codex_wait,
        "send_session_key",
        AsyncMock(side_effect=OSError("tmux disconnected")),
    )
    assert await codex_wait.dismiss_codex_wait_prompt(sess, popup(2)) is False
