"""Codex backend contract: rollout discovery, parsing, and tmux launch."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from ccbot import codex_session_io
from ccbot.bot.callbacks import dir_browser as dir_browser_cb
from ccbot.config import config
from ccbot.handlers.callback_data import CB_ARC_RESTORE
from ccbot.handlers import archive
from ccbot.handlers.history import render_archived_card_pages
from ccbot.handlers.menu import build_footer_keyboard, render_settings_text
from ccbot.session import SessionManager, session_manager
from ccbot.session_import import build_import_context
from ccbot.session_models import Session as BotSession
from ccbot.tmux_manager import TmuxManager
from ccbot.transcript_parser import TranscriptParser


def _line(kind: str, payload: dict) -> dict:
    return {"timestamp": "2026-01-01T00:00:00Z", "type": kind, "payload": payload}


def test_agent_backend_is_exposed_in_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_manager, "agent_backend", "codex")
    monkeypatch.setattr(
        session_manager,
        "get_user_settings",
        lambda _uid: {
            "language": "en",
            "live_lag": 4,
            "voice": "auto",
        },
    )

    text = render_settings_text(42)
    keyboard = build_footer_keyboard(42, screen="settings_agent")

    assert "Agent: `Codex`" in text
    assert keyboard is not None
    choices = keyboard.inline_keyboard[0]
    assert [button.callback_data for button in choices] == [
        "st:agent:claude",
        "st:agent:codex",
    ]
    assert choices[1].text.startswith("• ")


def test_codex_directory_trust_prompt_is_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screens = iter(
        [
            ["Starting Codex…"],
            [
                "Do you trust the contents of this directory?",
                "1. Yes, continue",
                "2. No, quit",
            ],
        ]
    )

    class Pane:
        def __init__(self) -> None:
            self.sent: list[tuple[str, bool]] = []

        def capture_pane(self) -> list[str]:
            return next(screens)

        def send_keys(self, value: str, enter: bool = True) -> None:
            self.sent.append((value, enter))

    pane = Pane()
    monkeypatch.setattr("ccbot.tmux_manager.time.sleep", lambda _delay: None)

    accepted = TmuxManager._accept_codex_directory_trust(pane)

    assert accepted is True
    assert pane.sent == [("", True)]


def test_codex_ready_prompt_is_not_auto_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Pane:
        def capture_pane(self) -> list[str]:
            return ["OpenAI Codex", "› Fix a bug"]

        def send_keys(self, _value: str, enter: bool = True) -> None:
            raise AssertionError("normal Codex input must not be auto-confirmed")

    monkeypatch.setattr("ccbot.tmux_manager.time.sleep", lambda _delay: None)

    assert TmuxManager._accept_codex_directory_trust(Pane()) is False


def test_codex_resume_uses_selected_current_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    screens = iter(
        [
            ["Starting Codex…"],
            [
                "Choose working directory to resume this session",
                "› 1. Use session directory (/old/project)",
                "  2. Use current directory (/safe/staging/project)",
                "Press enter to continue",
            ],
            ["OpenAI Codex", "›"],
        ]
    )

    class Pane:
        def __init__(self) -> None:
            self.sent: list[tuple[str, bool]] = []

        def capture_pane(self) -> list[str]:
            return next(screens)

        def send_keys(self, value: str, enter: bool = True) -> None:
            self.sent.append((value, enter))

    pane = Pane()
    monkeypatch.setattr("ccbot.tmux_manager.time.sleep", lambda _delay: None)

    accepted = TmuxManager._accept_codex_directory_trust(pane)

    assert accepted is True
    assert pane.sent == [("Down", False), ("", True)]


def test_claude_transcript_converts_to_portable_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "550e8400-e29b-41d4-a716-446655440000"
    workdir = tmp_path / "project"
    workdir.mkdir()
    project_dir = tmp_path / "claude-projects" / str(workdir).replace("/", "-")
    project_dir.mkdir(parents=True)
    transcript = project_dir / f"{sid}.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [{"type": "text", "text": "Fix the parser"}]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [{"type": "text", "text": "I found the bug."}],
                            "stop_reason": "end_turn",
                        },
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "claude-projects")
    monkeypatch.setattr(config, "config_dir", tmp_path / "ccbot")
    sess = BotSession(
        id="archive1",
        name="parser",
        workdir=str(workdir),
        claude_session_id=sid,
        backend="claude",
        state="archived",
    )

    context = build_import_context(sess, "codex")

    text = context.read_text()
    assert "Source agent: `claude`" in text
    assert "Target agent: `codex`" in text
    assert "## User\n\nFix the parser" in text
    assert "## Assistant\n\nI found the bug." in text


@pytest.mark.asyncio
async def test_restore_claude_archive_imports_into_codex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "save_state", lambda: None)
    mgr.agent_backend = "codex"
    mgr.wait_for_session_map_entry = AsyncMock(return_value=True)  # type: ignore[method-assign]
    mgr.mark_window_starting = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(archive, "session_manager", mgr)
    monkeypatch.setattr(config, "config_dir", tmp_path / "ccbot")
    monkeypatch.setattr(config, "claude_projects_path", tmp_path / "claude-projects")

    sid = "550e8400-e29b-41d4-a716-446655440000"
    workdir = tmp_path / "project"
    workdir.mkdir()
    project_dir = tmp_path / "claude-projects" / str(workdir).replace("/", "-")
    project_dir.mkdir(parents=True)
    (project_dir / f"{sid}.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": "Continue me"}]},
            }
        )
        + "\n"
    )
    sess = BotSession(
        id="old",
        name="old-claude",
        workdir=str(workdir),
        claude_session_id=sid,
        backend="claude",
        state="archived",
    )
    mgr.sessions[sess.id] = sess
    captured: dict[str, object] = {}

    async def create_window(*_args, **kwargs):
        captured.update(kwargs)
        ws = mgr.get_window_state("@9")
        ws.session_id = "660e8400-e29b-41d4-a716-446655440000"
        ws.cwd = str(workdir)
        ws.window_name = "project"
        return True, "ok", "project", "@9"

    tmux = MagicMock()
    tmux.create_window = create_window
    tmux.kill_window = AsyncMock()
    monkeypatch.setattr(archive, "tmux_manager", tmux)

    ok, message = await archive.restore_session(MagicMock(), 42, sess)

    assert ok is True
    assert captured["backend"] == "codex"
    assert captured["resume_session_id"] is None
    assert "Continue a session imported from claude" in str(captured["initial_prompt"])
    assert sess.backend == "codex"
    assert sess.claude_session_id == "660e8400-e29b-41d4-a716-446655440000"
    assert sess.imported_from_backend == "claude"
    assert sess.imported_from_session_id == sid
    assert "imported from claude" in message
    mgr.wait_for_session_map_entry.assert_awaited_once_with("@9", timeout=15.0)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_restore_codex_archive_does_not_wait_for_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "save_state", lambda: None)
    mgr.agent_backend = "codex"
    mgr.wait_for_session_map_entry = AsyncMock(return_value=False)  # type: ignore[method-assign]
    mgr.mark_window_starting = MagicMock()  # type: ignore[method-assign]
    monkeypatch.setattr(archive, "session_manager", mgr)

    sid = "550e8400-e29b-41d4-a716-446655440000"
    workdir = tmp_path / "project"
    workdir.mkdir()
    sess = BotSession(
        id="old",
        name="old-codex",
        workdir=str(workdir),
        claude_session_id=sid,
        backend="codex",
        state="archived",
    )
    mgr.sessions[sess.id] = sess

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        json.dumps({"type": "session_meta", "payload": {"id": sid}}) + "\n"
    )
    monkeypatch.setattr(config, "session_map_file", tmp_path / "session_map.json")
    monkeypatch.setattr(
        codex_session_io,
        "build_session_file_path",
        lambda _sid, _cwd: rollout,
    )

    tmux = MagicMock()
    tmux.create_window = AsyncMock(return_value=(True, "ok", "project", "@9"))
    monkeypatch.setattr(archive, "tmux_manager", tmux)

    ok, _message = await archive.restore_session(MagicMock(), 42, sess)

    assert ok is True
    mgr.mark_window_starting.assert_called_once_with(  # type: ignore[attr-defined]
        "@9", backend="codex", resume=True, bot=ANY, user_id=42
    )
    mgr.wait_for_session_map_entry.assert_not_awaited()  # type: ignore[attr-defined]
    ws = mgr.get_window_state("@9")
    assert ws.session_id == sid
    assert ws.cwd == str(workdir)
    assert ws.backend == "codex"
    assert ws.transcript_path == str(rollout)
    assert sess.window_id == "@9"
    session_map = json.loads(config.session_map_file.read_text())
    assert session_map["ccbot:@9"] == {
        "session_id": sid,
        "cwd": str(workdir),
        "window_name": "project",
        "backend": "codex",
        "transcript_path": str(rollout),
    }

    await mgr.load_session_map()
    assert mgr.get_window_state("@9").session_id == sid


@pytest.mark.asyncio
async def test_codex_picker_description_uses_user_message(tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    session = SimpleNamespace(
        session_id="codex-session",
        summary="investigate archive latency",
        file_path=str(rollout),
    )
    initial = await dir_browser_cb.resolve_session_summaries([session], user_id=42)

    assert initial == {"codex-session": "investigate archive latency"}


def test_codex_rollout_normalizes_text_and_tools() -> None:
    entries = [
        _line("event_msg", {"type": "user_message", "message": "fix it"}),
        _line(
            "response_item",
            {
                "type": "function_call",
                "name": "exec_command",
                "call_id": "call-1",
                "arguments": '{"cmd":"pytest"}',
            },
        ),
        _line(
            "response_item",
            {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "all green",
            },
        ),
        _line(
            "event_msg",
            {
                "type": "agent_message",
                "message": "Done.",
                "phase": "final_answer",
            },
        ),
    ]
    parsed, pending = TranscriptParser.parse_entries(entries)
    assert pending == {}
    assert [(item.role, item.content_type) for item in parsed] == [
        ("user", "text"),
        ("assistant", "tool_use"),
        ("assistant", "tool_result"),
        ("assistant", "text"),
    ]
    assert parsed[-1].text == "Done."
    assert parsed[-1].stop_reason == "end_turn"


def test_codex_pending_prompt_detection_only_matches_bottom_input() -> None:
    pane = "old output\n\n› send the report now\n\n  model · ~/project"
    assert TmuxManager._codex_prompt_contains(pane, "send the report now")
    assert not TmuxManager._codex_prompt_contains(pane, "different prompt")


def test_codex_pending_long_prompt_beyond_twenty_rows_is_detected() -> None:
    text = " ".join(f"word-{i}" for i in range(120))
    wrapped = "\n  ".join(" ".join(text.split()[i : i + 5]) for i in range(0, 120, 5))
    pane = f"old output\n\n› {wrapped}\n\n  model · ~/project"

    assert len(pane.splitlines()) > 20
    assert TmuxManager._codex_prompt_contains(pane, text)


def test_codex_completed_prompt_is_not_treated_as_pending() -> None:
    pane = "› send the report now\n\n• Working (2s)\n"
    assert not TmuxManager._codex_prompt_contains(pane, "send the report now")


@pytest.mark.asyncio
async def test_codex_pending_prompt_retries_until_it_is_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TmuxManager()
    pending = "old output\n\n› a long voice prompt\n\n  model · ~/project"
    manager.capture_pane = AsyncMock(side_effect=[pending, pending, "› "])
    manager.send_keys = AsyncMock(return_value=True)
    sleep = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", sleep)

    assert await manager.ensure_codex_prompt_submitted("@5", "a long voice prompt")
    assert manager.send_keys.await_count == 2
    manager.send_keys.assert_awaited_with("@5", "Enter", enter=False, literal=False)
    assert sleep.await_count == 3


@pytest.mark.asyncio
async def test_codex_pending_prompt_stops_after_bounded_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = TmuxManager()
    pending = "› voice prompt\n\n  model · ~/project"
    manager.capture_pane = AsyncMock(return_value=pending)
    manager.send_keys = AsyncMock(return_value=True)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    assert not await manager.ensure_codex_prompt_submitted("@5", "voice prompt")
    assert manager.send_keys.await_count == 3


@pytest.mark.asyncio
async def test_codex_rollout_discovery_uses_session_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    rollout = root / "2026" / "01" / "02" / "rollout-any-name.jsonl"
    rollout.parent.mkdir(parents=True)
    sid = "550e8400-e29b-41d4-a716-446655440000"
    rows = [
        _line("session_meta", {"id": sid, "cwd": str(tmp_path)}),
        _line("event_msg", {"type": "user_message", "message": "hello"}),
        _line(
            "event_msg",
            {"type": "agent_message", "message": "hi", "phase": "final_answer"},
        ),
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(config, "codex_sessions_path", root)

    found = await codex_session_io.get_session_direct(sid, str(tmp_path))
    assert found is not None
    assert found.file_path == str(rollout)
    assert found.summary == "hello"


@pytest.mark.asyncio
async def test_codex_rollout_exact_path_survives_resume_in_different_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    rollout = root / "2026" / "01" / "02" / "rollout-original.jsonl"
    rollout.parent.mkdir(parents=True)
    original_cwd = tmp_path / "original"
    resumed_cwd = tmp_path / "safe-copy"
    sid = "550e8400-e29b-41d4-a716-446655440000"
    rows = [
        _line("session_meta", {"id": sid, "cwd": str(original_cwd)}),
        _line("event_msg", {"type": "user_message", "message": "history"}),
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(config, "codex_sessions_path", root)

    without_path = await codex_session_io.get_session_direct(sid, str(resumed_cwd))
    with_path = await codex_session_io.get_session_direct(
        sid, str(resumed_cwd), rollout
    )

    assert without_path is None
    assert with_path is not None
    assert with_path.file_path == str(rollout)


@pytest.mark.asyncio
async def test_manager_resolves_restored_codex_rollout_by_exact_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
    mgr = SessionManager()
    monkeypatch.setattr(mgr, "save_state", lambda: None)
    root = tmp_path / "sessions"
    rollout = root / "2026" / "01" / "02" / "rollout-original.jsonl"
    rollout.parent.mkdir(parents=True)
    sid = "550e8400-e29b-41d4-a716-446655440000"
    rollout.write_text(
        json.dumps(_line("session_meta", {"id": sid, "cwd": "/original"})) + "\n"
    )
    monkeypatch.setattr(config, "codex_sessions_path", root)
    state = mgr.get_window_state("@9")
    state.session_id = sid
    state.cwd = "/safe-copy"
    state.backend = "codex"
    state.transcript_path = str(rollout)

    found = await mgr.resolve_session_for_window("@9")

    assert found is not None
    assert found.file_path == str(rollout)
    assert state.session_id == sid
    assert state.cwd == "/safe-copy"


@pytest.mark.asyncio
async def test_codex_archive_renders_final_answer_from_rollout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sessions"
    sid = "550e8400-e29b-41d4-a716-446655440000"
    rollout = root / "2026" / "07" / "31" / f"rollout-{sid}.jsonl"
    rollout.parent.mkdir(parents=True)
    rows = [
        _line("session_meta", {"id": sid, "cwd": str(tmp_path)}),
        _line(
            "event_msg",
            {
                "type": "agent_message",
                "message": "the archived final answer",
                "phase": "final_answer",
            },
        ),
    ]
    rollout.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    monkeypatch.setattr(config, "codex_sessions_path", root)
    sess = BotSession(
        id="archive-codex",
        name="archive",
        backend="codex",
        workdir=str(tmp_path),
        claude_session_id=sid,
        state="archived",
    )

    rendered = await render_archived_card_pages(sess)

    assert rendered is not None
    pages, _ = rendered
    assert "the archived final answer" in pages[-1]


@pytest.mark.asyncio
async def test_archive_restore_rebuilds_live_card_on_existing_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ccbot.bot.callbacks import archive as archive_callback

    sess = BotSession(id="restored", name="restored", state="archived")
    query = MagicMock()
    query.data = f"{CB_ARC_RESTORE}{sess.id}"
    query.message.message_id = 77
    query.answer = AsyncMock()
    context = MagicMock()
    context.bot = MagicMock()
    user = MagicMock(id=42)
    paint = AsyncMock()
    reset = MagicMock()
    monkeypatch.setattr(
        archive_callback.session_manager, "get_session", lambda _sid: sess
    )
    monkeypatch.setattr(
        archive_callback.session_manager, "set_last_switcher_msg", MagicMock()
    )
    monkeypatch.setattr(
        archive_callback, "restore_session", AsyncMock(return_value=(True, "ok"))
    )
    monkeypatch.setattr(archive_callback, "paint_card_on_carrier", paint)
    monkeypatch.setattr(archive_callback, "reset_card", reset)

    handled = await archive_callback.handle(query, context, user)

    assert handled is True
    reset.assert_called_once_with(42, "restored")
    paint.assert_awaited_once_with(context.bot, 42, sess, 77)


@pytest.mark.asyncio
async def test_tmux_builds_codex_resume_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []
    trust_started = threading.Event()
    release_trust = threading.Event()

    class Pane:
        def send_keys(self, value: str, enter: bool = True) -> None:
            assert enter is True
            sent.append(value)

    class Window:
        window_id = "@7"
        active_pane = Pane()

        def set_window_option(self, _name: str, _value: str) -> None:
            pass

    class Session:
        def new_window(self, **_kwargs):
            return Window()

    mgr = TmuxManager()
    monkeypatch.setattr(mgr, "get_or_create_session", lambda: Session())

    def wait_for_trust(_pane: object) -> bool:
        trust_started.set()
        release_trust.wait(timeout=2.0)
        return True

    monkeypatch.setattr(mgr, "_accept_codex_directory_trust", wait_for_trust)

    async def no_existing(_name: str):
        return None

    monkeypatch.setattr(mgr, "find_window_by_name", no_existing)
    monkeypatch.setattr(
        config, "codex_command", "/data/data/com.termux/files/usr/bin/codex"
    )
    monkeypatch.setattr(
        config,
        "codex_flags",
        "--dangerously-bypass-approvals-and-sandbox --no-alt-screen",
    )

    ok, _message, _name, wid = await mgr.create_window(
        str(tmp_path),
        resume_session_id="550e8400-e29b-41d4-a716-446655440000",
        owner_user_id=42,
        backend="codex",
    )
    assert ok is True
    assert wid == "@7"
    assert "CCBOT_AGENT_BACKEND=codex" in sent[0]
    assert f"CCBOT_DIR={config.config_dir}" in sent[0]
    assert "CCBOT_CHAT_ID=42" in sent[0]
    assert "/data/data/com.termux/files/usr/bin/codex" in sent[0]
    assert " resume 550e8400-e29b-41d4-a716-446655440000" in sent[0]
    assert "--resume" not in sent[0]
    # create_window returned even though its startup-prompt worker is still
    # blocked. Trust handling must never hold the Telegram callback open.
    assert await asyncio.to_thread(trust_started.wait, 1.0)
    startup_tasks = tuple(mgr._startup_tasks)
    assert len(startup_tasks) == 1
    assert not startup_tasks[0].done()
    release_trust.set()
    await asyncio.gather(*startup_tasks)
