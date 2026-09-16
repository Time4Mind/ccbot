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
    FakeCallbackQuery,
    FakeUpdate,
    FakeUser,
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
    # Live output is already consumed by the monitor offset. The historical
    # per-user window offset has no reader and must not force a full state.json
    # fsync for every streamed event.
    assert session_manager.user_window_offsets == {}


@pytest.mark.asyncio
async def test_second_final_turn_spawns_new_card_and_freezes_previous(
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

    # A second completed turn creates a new card and freezes the old one.
    append_jsonl(jsonl, assistant_turn("Second answer."))
    await _run_monitor_cycles(monitor, 1)

    assert fake_bot.send_message.call_count == sends_after_first + 1
    assert "Second answer" in fake_bot.sent_messages[-1].text
    assert "✅" in fake_bot.sent_messages[-1].text
    fake_bot.edit_message_reply_markup.assert_awaited_with(
        chat_id=USER_ID,
        message_id=fake_bot.sent_messages[-2].message_id,
        reply_markup=None,
    )

    # Automatic card refreshes must preserve the completion marker until the
    # user acknowledges this exact carrier with a button tap.
    from ccbot.handlers.notifications import refresh_panel, set_card_context_pct

    final_message = fake_bot.sent_messages[-1]
    set_card_context_pct(USER_ID, "cccc3333", 51)
    await refresh_panel(fake_bot, USER_ID, immediate=True)
    final_edits = [
        edit for edit in fake_bot.edits if edit["message_id"] == final_message.message_id
    ]
    assert final_edits and "✅" in final_edits[-1]["text"]

    # The first tap on that carrier acknowledges the marker even when the
    # selected control would otherwise be a no-op and not repaint the card.
    from ccbot.bot.callbacks import callback_handler
    from ccbot.handlers.callback_data import CB_SW_NOOP

    class _Ctx:
        bot = fake_bot
        user_data: dict = {}

    user = FakeUser(USER_ID)
    query = FakeCallbackQuery(
        data=CB_SW_NOOP,
        user=user,
        message_id=final_message.message_id,
        chat_id=USER_ID,
        bot=fake_bot,
    )
    await callback_handler(
        FakeUpdate(user=user, callback_query=query),
        _Ctx(),
    )
    final_edits = [
        edit for edit in fake_bot.edits if edit["message_id"] == final_message.message_id
    ]
    assert "✅" not in final_edits[-1]["text"]
