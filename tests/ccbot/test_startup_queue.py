from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from telegram.ext import ApplicationHandlerStop

from ccbot.session import SessionManager
from ccbot.session_models import Session, WindowState
from ccbot.startup_queue import (
    _replay,
    begin_startup_queue,
    bind_startup_queue,
    capture_startup_message,
    enqueue_startup_message,
    has_startup_queue,
    pending_startup_count,
    reset_startup_queues_for_test,
)


def _update(
    seq: int,
    *,
    text: str | None = None,
    voice: bool = False,
    photo: bool = False,
    document: bool = False,
) -> MagicMock:
    message = SimpleNamespace(
        message_id=seq,
        text=text,
        voice=object() if voice else None,
        photo=[object()] if photo else [],
        document=object() if document else None,
    )
    update = MagicMock()
    update.effective_user = SimpleNamespace(id=42)
    update.message = message
    return update


@pytest.fixture(autouse=True)
def _clean_queue() -> None:
    reset_startup_queues_for_test()
    yield
    reset_startup_queues_for_test()


@pytest.mark.asyncio
async def test_capture_stops_old_session_routing_and_preserves_order() -> None:
    context = MagicMock()
    begin_startup_queue(42)

    with pytest.raises(ApplicationHandlerStop):
        await capture_startup_message(_update(10, text="first"), context)
    with pytest.raises(ApplicationHandlerStop):
        await capture_startup_message(_update(11, voice=True), context)

    assert pending_startup_count(42) == 2


@pytest.mark.asyncio
async def test_auth_code_bypasses_agent_startup_queue() -> None:
    context = MagicMock()
    begin_startup_queue(42)
    with patch("ccbot.codex_auth.get_flow", return_value=object()):
        await capture_startup_message(_update(10, text="one-time-code"), context)
    assert pending_startup_count(42) == 0


@pytest.mark.asyncio
async def test_new_command_can_retry_failed_creation_flow() -> None:
    context = MagicMock()
    begin_startup_queue(42)
    await capture_startup_message(_update(10, text="/new retry /tmp"), context)
    assert pending_startup_count(42) == 0


@pytest.mark.asyncio
async def test_drain_includes_messages_arriving_while_window_becomes_ready() -> None:
    context = MagicMock()
    begin_startup_queue(42)
    enqueue_startup_message(_update(1, text="first"), context)

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    seen: list[int] = []
    sess = SimpleNamespace(id="fresh")
    surface = MagicMock()

    async def replay(entry, window_id) -> bool:
        assert window_id == "@9"
        seen.append(entry.sequence)
        if entry.sequence == 1:
            first_started.set()
            await release_first.wait()
        return True

    with (
        patch(
            "ccbot.session.session_manager.wait_for_window_ready",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ccbot.session.session_manager.find_session_by_window",
            return_value=sess,
        ),
        patch(
            "ccbot.handlers.notifications.schedule_card_after_message",
            new=surface,
        ),
        patch("ccbot.startup_queue._replay", side_effect=replay),
    ):
        task = bind_startup_queue(42, "@9")
        assert task is not None
        await first_started.wait()
        enqueue_startup_message(_update(2, text="second"), context)
        release_first.set()
        await task

    assert seen == [1, 2]
    surface.assert_called_once_with(context.bot, 42, sess, 2)
    assert not has_startup_queue(42)


@pytest.mark.asyncio
async def test_unconfirmed_head_does_not_stall_everything_behind_it() -> None:
    context = MagicMock()
    begin_startup_queue(42)
    enqueue_startup_message(_update(1, text="first"), context)
    enqueue_startup_message(_update(2, text="second"), context)

    with (
        patch(
            "ccbot.session.session_manager.wait_for_window_ready",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ccbot.startup_queue._replay",
            new=AsyncMock(side_effect=[False, True]),
        ) as replay,
    ):
        task = bind_startup_queue(42, "@9")
        assert task is not None
        await task

    assert replay.await_count == 2
    assert not has_startup_queue(42)
    assert pending_startup_count(42) == 0


@pytest.mark.asyncio
async def test_replay_pins_every_handler_to_bound_window() -> None:
    context = MagicMock()
    entry = SimpleNamespace(
        update=_update(1, text="follow-up"), context=context, sequence=1
    )
    target = AsyncMock(return_value=True)

    with patch("ccbot.bot.messages.text_handler", new=target):
        assert await _replay(entry, "@A")

    target.assert_awaited_once_with(entry.update, context, pinned_wid="@A")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "handler"),
    [
        (_update(1, text="hello"), "text_handler"),
        (_update(2, text="/compact"), "forward_command_handler"),
        (_update(3, voice=True), "voice_handler"),
        (_update(4, photo=True), "photo_handler"),
        (_update(5, document=True), "document_handler"),
        (_update(6), "unsupported_content_handler"),
    ],
)
async def test_replay_covers_every_inbound_kind(update, handler: str) -> None:
    context = MagicMock()
    entry = SimpleNamespace(update=update, context=context, sequence=1)
    target = AsyncMock(return_value=True)
    with patch(f"ccbot.bot.messages.{handler}", new=target):
        assert await _replay(entry)
    target.assert_awaited_once_with(update, context)


def test_shell_prompt_is_not_codex_readiness() -> None:
    assert not SessionManager._pane_has_ready_input("zsh\n› codex", "codex")
    assert SessionManager._pane_has_ready_input(
        "│ >_ OpenAI Codex (v0.146.0) │\n\n› Ask anything", "codex"
    )


def test_resumed_codex_without_visible_header_is_ready() -> None:
    pane = (
        "• Previous assistant output after a long restored transcript\n\n"
        "› Improve documentation in @filename\n\n"
        "  gpt-5.6-sol high · ~/pet_projects/ccbot"
    )

    assert SessionManager._pane_has_ready_input(pane, "codex")


@pytest.mark.asyncio
async def test_session_map_poll_keeps_fresh_bound_window_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ccbot.config import config

    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    monkeypatch.setattr(SessionManager, "save_state", lambda self: None)
    session_map = tmp_path / "session_map.json"
    session_map.write_text("{}")
    monkeypatch.setattr(config, "session_map_file", session_map)
    manager = SessionManager()
    manager.window_states["@9"] = WindowState(backend="codex", cwd=str(tmp_path))
    manager.sessions["fresh"] = Session(
        id="fresh", name="fresh", window_id="@9", backend="codex"
    )

    await manager.load_session_map()

    assert "@9" in manager.window_states


@pytest.mark.asyncio
async def test_first_turn_can_be_confirmed_after_binding_appears(
    tmp_path: Path,
) -> None:
    from ccbot.bot.messages import _wait_for_voice_transcript

    transcript = tmp_path / "rollout.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "first prompt"},
            }
        )
        + "\n"
    )
    state = WindowState(
        session_id="sid",
        cwd=str(tmp_path),
        window_name="new",
        backend="codex",
        transcript_path=str(transcript),
    )
    fake_manager = MagicMock()
    fake_manager.window_states = {"@9": state}
    fake_manager.load_session_map = AsyncMock()

    with patch("ccbot.bot.messages.session_manager", fake_manager):
        assert await _wait_for_voice_transcript(None, "first prompt", wid="@9")


@pytest.mark.asyncio
async def test_late_transcript_ack_never_retypes_an_accepted_prompt() -> None:
    from ccbot.bot.messages import _send_with_delivery_proof

    fake_manager = MagicMock()
    fake_manager.send_to_window = AsyncMock(return_value=(True, "Sent"))
    fake_session = SimpleNamespace(backend="codex")
    transcript_wait = AsyncMock(return_value=False)
    with (
        patch("ccbot.bot.messages.session_manager", fake_manager),
        patch(
            "ccbot.bot.messages.tmux_manager.ensure_codex_prompt_submitted",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ccbot.bot.messages._wait_for_voice_transcript",
            new=transcript_wait,
        ),
        patch("ccbot.bot.messages._voice_transcript_checkpoint", return_value=None),
    ):
        ok, _ = await _send_with_delivery_proof("@9", "do it", fake_session)

    assert ok
    assert fake_manager.send_to_window.await_count == 1
    transcript_wait.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_left_in_input_fails_without_retyping_text() -> None:
    from ccbot.bot.messages import _send_with_delivery_proof

    fake_manager = MagicMock()
    fake_manager.send_to_window = AsyncMock(return_value=(True, "Sent"))
    fake_session = SimpleNamespace(backend="codex")
    with (
        patch("ccbot.bot.messages.session_manager", fake_manager),
        patch(
            "ccbot.bot.messages.tmux_manager.ensure_codex_prompt_submitted",
            new=AsyncMock(return_value=False),
        ),
        patch("ccbot.bot.messages._voice_transcript_checkpoint", return_value=None),
    ):
        ok, message = await _send_with_delivery_proof("@9", "do it", fake_session)

    assert not ok
    assert "input field" in message
    assert fake_manager.send_to_window.await_count == 1
