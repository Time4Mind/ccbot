"""Native OpenCode messages through the monitor, retry transport and card."""

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest

from ccbot import opencode_input, opencode_tls_retry as retries
from ccbot.bot.session_events import handle_new_message
from ccbot.config import config
from ccbot.opencode_receipt import native_checkpoint
from ccbot.opencode_session_io import ensure_projection
from ccbot.session import session_manager
from ccbot.session_monitor import SessionMonitor
from ccbot.tmux_manager import tmux_manager
from ccbot.utils import ccbot_dir
from harness import USER_ID, seed_session


TLS = "EPROTO: The TLS handshake failed. Does the server speak TLS on this port?"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "pasted",
        "legacy_paste",
        "different_text",
        "later_same",
        "exhaust",
        "stop",
        "new",
        "close",
        "tool",
        "other_error",
        "native_input",
        "ambiguous",
    ],
)
async def test_retry_one_logical_voice_turn(
    mode, opencode_native, fake_bot, no_card_lag, projects_path, monkeypatch
):
    db, dbpath, cwd = opencode_native
    sid, wid = "ses_ccbotAcceptance123", "@8"
    text = "Retry  check\nline two"
    sent = (
        " \tRetry  check\r\nline two  " if mode in ("pasted", "legacy_paste") else text
    )
    sess = seed_session(
        session_manager,
        sid="tls" + mode,
        name="TLS",
        window_id=wid,
        workdir=str(cwd),
        claude_session_id=sid,
        active_for=USER_ID,
    )
    sess.backend = "opencode"
    path = ensure_projection(sid, str(cwd))
    mapping = ccbot_dir() / "session_map.json"
    key = f"{config.tmux_session_name}:{wid}"
    mapping.write_text(
        json.dumps(
            {
                key: dict(
                    backend="opencode",
                    session_id=sid,
                    cwd=str(cwd),
                    transcript_path=str(path),
                )
            }
        )
    )
    monkeypatch.setattr(config, "session_map_file", mapping)
    await session_manager.load_session_map()
    monitor = SessionMonitor(
        projects_path=projects_path, state_file=cwd / "monitor.json"
    )

    async def poll():
        ids, targets = monitor._current_session_targets()
        msgs = await monitor.check_for_updates(set(ids.values()), session_infos=targets)
        for msg in msgs:
            await handle_new_message(msg, fake_bot)
        return msgs

    ledger = retries.TLSRetries(cwd / "retry.json")
    monkeypatch.setattr(retries, "tls_retries", ledger)
    assert 10 <= retries.DELAY <= 15
    delay = (
        12
        if mode == "success" and os.environ.get("CCBOT_TEST_REAL_RETRY_DELAY")
        else 0.05
    )
    monkeypatch.setattr(retries, "DELAY", delay)
    request = SimpleNamespace(id="123:456", kind="voice", session_id=sess.id)
    ledger.admit(sess, request.id)
    checkpoint = native_checkpoint(wid, sess.id)
    assert checkpoint
    ledger.prepare(request, checkpoint, sent)
    if mode == "legacy_paste":
        # On-disk chain written by the previous release.
        saved = json.loads(ledger.path.read_text())
        saved["chains"][sess.id]["payload_hash"] = hashlib.sha256(
            sent.replace("\r\n", "\n").encode()
        ).hexdigest()
        ledger.path.write_text(json.dumps(saved))
        ledger = retries.TLSRetries(ledger.path)
        monkeypatch.setattr(retries, "tls_retries", ledger)
    await poll()
    pastes = []
    seq = 0

    def record(role, parts, **metadata):
        nonlocal seq
        seq += 1
        mid = f"msg{seq}"
        data = dict(role=role, time={"created": seq, "completed": seq}, **metadata)
        db.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)",
            (mid, sid, seq, seq, json.dumps(data)),
        )
        for i, part in enumerate(parts):
            db.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"p{seq}{i}", mid, sid, seq, seq, json.dumps(part)),
            )
        db.commit()
        return mid

    def user(value):
        return record("user", [dict(type="text", text=value)])

    def error(parent, kind="UnknownError"):
        return record(
            "assistant",
            [],
            parentID=parent,
            error=dict(name=kind, data=dict(message=TLS)),
        )

    async def transport(window, payload, *, backend):
        assert (window, payload, backend) == (wid, text, "opencode")

        # The real OpenCode transport invokes the callback immediately before paste.
        def paste(begin):
            assert begin()
            pastes.append(asyncio.get_running_loop().time())
            return user(payload + " ")

        async def native_send(begin):
            parent = paste(begin)
            if mode == "success" or (
                mode in ("pasted", "legacy_paste") and len(pastes) == 2
            ):
                record(
                    "assistant",
                    [dict(type="text", text="ANSWER OK")],
                    parentID=parent,
                    finish="stop",
                )
            else:
                error(parent)
            return True

        return await opencode_input.submit(window, payload, native_send)

    monkeypatch.setattr(
        opencode_input,
        "_capture",
        lambda _: asyncio.sleep(
            0,
            result="  ┃\n  ┃  Build · GPT-6 Luna OpenAI\n  /tmp/lab  24K (2%)  ctrl+p commands\n",
        ),
    )
    monkeypatch.setattr(tmux_manager, "send_keys", transport)
    native = sent.replace("\r\n", "\n") + " "
    if mode == "different_text":
        native = native.replace("Retry  check", "Retry check")
    parent = user(native)
    if mode == "tool":
        record(
            "assistant",
            [
                dict(
                    type="tool",
                    id="tool",
                    tool="bash",
                    callID="call",
                    state=dict(status="completed", input={}, output="ok"),
                )
            ],
            parentID=parent,
            finish="tool-calls",
        )
    error(parent, "APIError" if mode == "other_error" else "UnknownError")
    if mode == "later_same":
        user(native)
    events = await poll()
    if mode in ("tool", "other_error", "different_text", "later_same"):
        assert not ledger.progress(sess) and not pastes
        assert any("EPROTO" in m.text for m in fake_bot.sent_messages)
        return

    assert ledger.progress(sess) == 1
    await handle_new_message(events[-1], fake_bot)
    assert ledger.progress(sess) == 1
    # Restart before the scheduled paste; preserve its deadline and budget.
    await ledger.shutdown()
    ledger = retries.TLSRetries(ledger.path)
    monkeypatch.setattr(retries, "tls_retries", ledger)
    ledger.restore(fake_bot)
    if mode == "stop":
        ledger.cancel(sess)
    elif mode == "new":
        ledger.admit(sess, "123:457")
    elif mode == "close":
        session_manager.mark_session_archived(sess.id)
    elif mode == "native_input":
        user("Совсем другой запрос")
        await poll()
    elif mode == "ambiguous":
        # Crash between reserving paste and native acknowledgement.
        await ledger.shutdown()
        chain = ledger.chains[sess.id]
        chain.phase = "sent"
        chain.floor = seq
        ledger.save()
        ledger = retries.TLSRetries(ledger.path)
        monkeypatch.setattr(retries, "tls_retries", ledger)
        ledger.restore(fake_bot)
    start = asyncio.get_running_loop().time()
    try:
        restarted = False
        async with asyncio.timeout(delay * 11 + 8):
            while ledger.tasks:
                await asyncio.sleep(0.015)
                events = await poll()
                for msg in events:
                    if msg.api_error == "opencode_tls_handshake":
                        await handle_new_message(msg, fake_bot)
                if mode == "exhaust" and ledger.progress(sess) == 5 and not restarted:
                    restarted = True
                    await ledger.shutdown()
                    ledger = retries.TLSRetries(ledger.path)
                    monkeypatch.setattr(retries, "tls_retries", ledger)
                    ledger.restore(fake_bot)
            await poll()
        texts = [m.text for m in fake_bot.sent_messages]
        if mode in ("success", "pasted", "legacy_paste"):
            assert len(pastes) == (1 if mode == "success" else 2)
            assert pastes[0] - start >= delay - 0.2
            assert sum(t.count("ANSWER OK") for t in texts) == 1
            assert all("EPROTO" not in t for t in texts)
        elif mode == "exhaust":
            assert restarted
            updates = [
                e for e in fake_bot.edits if "Переподключение" in e.get("text", "")
            ]
            assert updates and len({e["message_id"] for e in updates}) == 1
            assert len(pastes) == 10
            assert all(b - a >= delay - 0.01 for a, b in zip(pastes, pastes[1:]))
            assert sum(t.count("EPROTO") for t in texts) == 1, texts
        else:
            assert not pastes
        assert not ledger.progress(sess)
    finally:
        await ledger.shutdown()
