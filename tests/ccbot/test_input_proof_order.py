"""Distinct physical user rows acknowledge inputs independently across restart."""

import json
from datetime import datetime, timezone

from ccbot.input_recovery import InputRecovery, InputRequest
from ccbot.session import Session, WindowState, session_manager


def row(text, second=2):
    return (
        json.dumps(
            {
                "timestamp": f"2026-11-03T08:00:{second:02d}Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )
        + "\n"
    )


def source(monkeypatch, path):
    sess = Session(
        id="s",
        name="LocalBT",
        window_id="@1",
        backend="codex",
        claude_session_id="native",
    )
    monkeypatch.setattr(session_manager, "sessions", {"s": sess})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native", backend="codex", transcript_path=str(path)
            )
        },
    )
    return sess


def request(path, number, text):
    return InputRequest(
        f"42:{number}",
        42,
        number,
        "s",
        "@1",
        "text",
        {},
        state="unknown",
        strict_codex_proof=True,
        prepared=text,
        checkpoint={"path": str(path), "offset": 0, "backend": "codex"},
    )


def test_later_confirmation_does_not_hide_an_earlier_request_after_restart(
    tmp_path, monkeypatch
):
    path = tmp_path / "native.jsonl"
    path.write_text(row("first") + row("second", 3))
    source(monkeypatch, path)
    recovery = InputRecovery(tmp_path / "journal")
    first, second = request(path, 1, "first"), request(path, 2, "second")
    recovery.records = {first.id: first, second.id: second}
    assert recovery.reconcile(second)
    restarted = InputRecovery(recovery.path)
    restarted.load()
    assert restarted.reconcile(restarted.records[first.id])
    duplicate = request(path, 3, "second")
    restarted.records[duplicate.id] = duplicate
    assert not restarted.reconcile(duplicate)
    with path.open("a") as file:
        file.write(row("second", 4))
    assert restarted.reconcile(duplicate)


def test_unbound_transcript_ignores_historical_identical_text(tmp_path, monkeypatch):
    path = tmp_path / "native.jsonl"
    path.write_text(row("same", 1))
    source(monkeypatch, path)
    recovery = InputRecovery(tmp_path / "journal")
    item = request(path, 1, "same")
    item.checkpoint = {"path": "", "offset": 0, "backend": "codex", "unbound": True}
    item.dispatch_started_at = datetime(
        2026, 11, 3, 8, 0, 2, tzinfo=timezone.utc
    ).timestamp()
    recovery.records[item.id] = item
    assert not recovery.reconcile(item)
    recovery.save()
    restarted = InputRecovery(recovery.path)
    restarted.load()
    with path.open("a") as file:
        file.write(row("same", 3))
    assert restarted.reconcile(restarted.records[item.id])


def test_legacy_watermark_does_not_hide_an_unconfirmed_distinct_row(
    tmp_path, monkeypatch
):
    path = tmp_path / "native.jsonl"
    path.write_text(row("first") + row("second", 3))
    source(monkeypatch, path)
    recovery = InputRecovery(tmp_path / "journal")
    recovery.confirmed_through[str(path)] = path.stat().st_size
    first = request(path, 1, "first")
    recovery.records[first.id] = first
    assert recovery.reconcile(first)
    second = request(path, 2, "second")
    recovery.records[second.id] = second
    assert not recovery.reconcile(second)


async def test_archived_source_reconciles_received_input_without_resending(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    path = tmp_path / "native.jsonl"
    path.write_text(row("already received"))
    sess = source(monkeypatch, path)
    sess.state = "archived"
    sess.window_id = ""
    recovery = InputRecovery(tmp_path / "journal")
    item = request(path, 1, "already received")
    recovery.records[item.id] = item
    processor = AsyncMock()
    result = await recovery.run(item.id, processor, object(), SimpleNamespace(), "@1")
    assert result and item.confirmed
    processor.assert_not_awaited()
