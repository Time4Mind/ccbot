"""A background OpenCode task must leave the root's live card subscribed."""

import io
import json
from types import SimpleNamespace

import pytest

from ccbot import hook
from ccbot.bot.session_events import handle_new_message
from ccbot.config import config
from ccbot.opencode_session_io import ensure_projection
from ccbot.session import session_manager
from ccbot.session_monitor import SessionMonitor
from harness import USER_ID, seed_session


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_writer", [False, True])
async def test_final_reaches_card_after_subagent_without_new_prompt(
    legacy_writer,
    fake_tmux,
    fake_bot,
    no_card_lag,
    projects_path,
    tmp_path,
    monkeypatch,
    opencode_native,
):
    window = "@151"
    root, child, nested = "ses_ccbotAcceptance123", "ses_child", "ses_nested"
    db, _, _ = opencode_native
    mapping = tmp_path / "session_map.json"
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("TMUX_PANE", "%9")
    monkeypatch.setattr(config, "session_map_file", mapping)
    monkeypatch.setattr(hook.os, "getppid", lambda: 40)
    monkeypatch.setattr(
        hook.subprocess,
        "run",
        lambda args, **kw: SimpleNamespace(
            stdout="40 30 40 40 opencode\n"
            if args[0] == "ps"
            else "/dev/pts/9|ccbot::@151:test\n",
        ),
    )
    fake_tmux.add_window(window, name="test", cwd=str(tmp_path))
    owner = seed_session(
        session_manager,
        sid=("aa000001" if legacy_writer else "aa000002"),
        name="test",
        window_id=window,
        workdir=str(tmp_path),
        active_for=USER_ID,
    )
    owner.backend = "opencode"
    with db:
        db.executemany(
            "INSERT INTO session(id,directory,title,parent_id,time_created,time_updated) VALUES (?, ?, 'test', ?, 1, 1)",
            [
                (sid, str(tmp_path), parent)
                for sid, parent in [(child, root), (nested, child), ("ses_new", None)]
            ],
        )

    def submit(sid):
        payload = dict(
            session_id=sid,
            cwd=str(tmp_path),
            hook_event_name="UserPromptSubmit",
            opencode_origin="chat.message",
        )
        monkeypatch.setattr(hook.sys, "stdin", io.StringIO(json.dumps(payload)))
        hook._process_hook_event(backend="opencode")

    monitor = SessionMonitor(
        projects_path=projects_path, state_file=tmp_path / "monitor.json"
    )

    async def poll():
        await session_manager.load_session_map()
        ids, targets = monitor._current_session_targets()
        for msg in await monitor.check_for_updates(
            set(ids.values()), session_infos=targets
        ):
            await handle_new_message(msg, fake_bot)

    submit(root)
    await poll()
    original = mapping.read_bytes()
    if legacy_writer:
        raw = json.loads(original)
        raw["ccbot:@151"].update(
            session_id=nested,
            transcript_path=str(ensure_projection(nested, str(tmp_path))),
        )
        mapping.write_text(json.dumps(raw))
    else:
        for sid in (child, nested, child):
            submit(sid)
        assert mapping.read_bytes() == original

    with db:
        for sid, text in [(root, "Parent final"), (nested, "Private child answer")]:
            msg = {
                "role": "assistant",
                "finish": "stop",
                "time": {"created": 2, "completed": 3},
            }
            db.execute(
                "INSERT INTO message VALUES (?, ?, 2, 3, ?)",
                (sid, sid, json.dumps(msg)),
            )
            part = {"type": "text", "text": text}
            db.execute(
                "INSERT INTO part VALUES (?, ?, ?, 2, 3, ?)",
                (sid, sid, sid, json.dumps(part)),
            )
    await poll()
    await poll()
    assert owner.claude_session_id == root
    assert json.loads(mapping.read_text())["ccbot:@151"]["session_id"] == root
    texts = [message.text for message in fake_bot.sent_messages]
    assert sum(t.count("Parent final") for t in texts) == 1
    assert all("Private child answer" not in t for t in texts)
    submit("ses_new")
    await poll()
    assert owner.claude_session_id == "ses_new"
