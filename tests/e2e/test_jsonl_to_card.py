"""E2E: JSONL transcript growth → live-card render via the real monitor.

Wires a real :class:`ccbot.session_monitor.SessionMonitor` (tmpdir projects
path, tiny poll interval) to the real outbound dispatcher
``bot.session_events.handle_new_message``. We append assistant turns to a
JSONL fixture and run the monitor a couple of cycles; the card machinery must
push the rendered text to Telegram (``send_message`` for the first card,
``edit_message_text`` once a card msg_id exists).
"""

from __future__ import annotations

import pytest

from ccbot.session import session_manager
from ccbot.session_monitor import SessionMonitor

from harness import (
    USER_ID,
    append_jsonl,
    assistant_turn,
    make_jsonl_path,
    seed_session,
    user_turn,
    write_session_map,
)

CLAUDE_SID = "33333333-3333-3333-3333-333333333333"
WORKDIR = "/tmp/proj"
WINDOW_ID = "@100"


async def _run_monitor_cycles(monitor: SessionMonitor, n: int) -> None:
    """Drive the monitor's internal steps deterministically for N cycles
    without the background sleep loop."""
    await monitor._cleanup_all_stale_sessions()
    monitor._last_session_map = await monitor._load_current_session_map()
    for _ in range(n):
        await session_manager.load_session_map()
        current_map = await monitor._detect_and_cleanup_changes()
        active_ids = set(current_map.values())
        new_messages = await monitor.check_for_updates(active_ids)
        for msg in new_messages:
            if monitor._message_callback:
                await monitor._message_callback(msg)


@pytest.mark.asyncio
async def test_assistant_turn_renders_card(
    fake_tmux, fake_bot, projects_path, no_card_lag, tmp_path
):
    from ccbot.config import config

    # Live window whose cwd matches the project dir (the monitor only scans
    # projects with an active tmux window at that cwd).
    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR)
    seed_session(
        session_manager,
        sid="cccc3333",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )
    write_session_map(
        config.session_map_file,
        window_id=WINDOW_ID,
        claude_session_id=CLAUDE_SID,
        cwd=WORKDIR,
    )

    jsonl = make_jsonl_path(projects_path, WORKDIR, CLAUDE_SID)
    # Seed an initial user turn so the file exists; the first monitor cycle
    # starts tracking at EOF (no replay of pre-existing history).
    append_jsonl(jsonl, user_turn("do a thing", cwd=WORKDIR))

    monitor = SessionMonitor(
        projects_path=projects_path,
        poll_interval=0.01,
        state_file=tmp_path / "monitor_state.json",
    )

    async def _cb(msg):
        from ccbot.bot.session_events import handle_new_message

        await handle_new_message(msg, fake_bot)

    monitor.set_message_callback(_cb)

    # Cycle 1: establish tracking at current EOF (no card yet).
    await _run_monitor_cycles(monitor, 1)
    assert fake_bot.send_message.call_count == 0

    # Append a completed assistant turn, then run another cycle: the
    # end-of-turn text routes through finalize_task → fresh card sent.
    append_jsonl(jsonl, assistant_turn("Build finished: 0 errors."))
    await _run_monitor_cycles(monitor, 1)

    assert fake_bot.send_message.call_count >= 1
    sent_texts = [m.text for m in fake_bot.sent_messages]
    # The body is MarkdownV2-rendered, so the trailing "." is escaped to "\.".
    assert any("Build finished: 0 errors" in t for t in sent_texts), sent_texts


@pytest.mark.asyncio
async def test_second_turn_edits_existing_card(
    fake_tmux, fake_bot, projects_path, no_card_lag, tmp_path
):
    from ccbot.config import config

    fake_tmux.add_window(WINDOW_ID, name="proj", cwd=WORKDIR)
    seed_session(
        session_manager,
        sid="cccc3333",
        name="proj",
        window_id=WINDOW_ID,
        workdir=WORKDIR,
        claude_session_id=CLAUDE_SID,
        active_for=USER_ID,
    )
    write_session_map(
        config.session_map_file,
        window_id=WINDOW_ID,
        claude_session_id=CLAUDE_SID,
        cwd=WORKDIR,
    )
    jsonl = make_jsonl_path(projects_path, WORKDIR, CLAUDE_SID)
    append_jsonl(jsonl, user_turn("first", cwd=WORKDIR))

    monitor = SessionMonitor(
        projects_path=projects_path,
        poll_interval=0.01,
        state_file=tmp_path / "monitor_state.json",
    )

    async def _cb(msg):
        from ccbot.bot.session_events import handle_new_message

        await handle_new_message(msg, fake_bot)

    monitor.set_message_callback(_cb)

    await _run_monitor_cycles(monitor, 1)
    append_jsonl(jsonl, assistant_turn("First answer."))
    await _run_monitor_cycles(monitor, 1)
    sends_after_first = fake_bot.send_message.call_count
    assert sends_after_first >= 1

    # A second completed turn edits the SAME card msg in place (no new send).
    append_jsonl(jsonl, assistant_turn("Second answer."))
    await _run_monitor_cycles(monitor, 1)

    assert fake_bot.edit_message_text.call_count >= 1
    edit_texts = [e["text"] for e in fake_bot.edits]
    assert any("Second answer" in t for t in edit_texts), edit_texts
    # No spurious extra card was spawned for the second turn.
    assert fake_bot.send_message.call_count == sends_after_first
