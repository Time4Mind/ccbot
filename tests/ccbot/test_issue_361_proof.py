import json
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.input_recovery import InputRecovery, InputRequest
from ccbot.handlers import bg_status
from ccbot.session import Session, WindowState, session_manager


def row(text, stamp):
    return (
        json.dumps(
            {
                "timestamp": stamp,
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


def test_missing_checkpoint_recovers_without_matching_old_identical_request(
    tmp_path, monkeypatch
):
    source = Session(
        id="s", name="s", window_id="@1", backend="codex", claude_session_id="native"
    )
    path = tmp_path / "native.jsonl"
    path.write_text(row("same", "2026-10-30T10:00:00Z"))
    monkeypatch.setattr(session_manager, "sessions", {"s": source})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native",
                backend="codex",
                cwd=str(tmp_path),
                transcript_path=str(path),
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal")
    request = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {},
        state="unknown",
        prepared="same",
        strict_codex_proof=True,
    )
    request.dispatch_started_at = datetime(
        2026, 10, 30, 10, 0, 1, tzinfo=timezone.utc
    ).timestamp()
    recovery.records[request.id] = request
    assert not recovery.reconcile(request)
    with path.open("a") as file:
        file.write(row("same", "2026-10-30T10:00:02Z"))
    assert recovery.reconcile(request)
    assert request.id not in recovery.records


def test_late_binding_recovers_distinct_identical_requests_after_restart(
    tmp_path, monkeypatch
):
    source = Session(
        id="s", name="s", window_id="@1", backend="codex", claude_session_id="native"
    )
    path = tmp_path / "native.jsonl"
    path.write_text(
        row("same", "2026-10-30T10:00:00Z") + row("same", "2026-10-30T10:00:02Z")
    )
    monkeypatch.setattr(session_manager, "sessions", {"s": source})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native",
                backend="codex",
                cwd=str(tmp_path),
                transcript_path=str(path),
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal")
    for n in (1, 2):
        request = InputRequest(
            f"42:{n}",
            42,
            n,
            "s",
            "@1",
            "text",
            {},
            state="unknown",
            prepared="same",
            strict_codex_proof=True,
            checkpoint={
                "path": str(tmp_path / "stale"),
                "offset": 0,
                "backend": "codex",
            },
            dispatch_started_at=datetime(
                2026, 10, 30, 10, 0, 1, tzinfo=timezone.utc
            ).timestamp(),
        )
        recovery.records[request.id] = request
    recovery.save()
    restored = InputRecovery(recovery.path)
    restored.load()
    assert restored.reconcile(restored.records["42:1"])
    assert not restored.reconcile(restored.records["42:2"])
    with path.open("a") as file:
        file.write(row("same", "2026-10-30T10:00:03Z"))
    assert restored.reconcile(restored.records["42:2"])


def test_busy_session_does_not_emit_unconfirmed_warning(tmp_path, monkeypatch):
    monkeypatch.setattr(bg_status, "_bg", {})
    bg_status.update_status(42, "s", "working")
    recovery = InputRecovery(tmp_path / "journal")
    recovery.clock = lambda: 100.0
    request = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {},
        state="awaiting_confirmation",
        prepared="request",
        strict_codex_proof=True,
        confirmation_deadline=90.0,
    )
    recovery.records[request.id] = request
    recovery.check_confirmation(request)
    assert request.state == "awaiting_confirmation"
    assert not request.notice


def test_replaced_transcript_recovers_after_checkpoint(tmp_path, monkeypatch):
    source = Session(
        id="s", name="s", window_id="@1", backend="codex", claude_session_id="native"
    )
    path = tmp_path / "native.jsonl"
    path.write_text(row("old", "2026-10-30T10:00:00Z"))
    stat = path.stat()
    monkeypatch.setattr(session_manager, "sessions", {"s": source})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native",
                backend="codex",
                cwd=str(tmp_path),
                transcript_path=str(path),
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal")
    request = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {},
        state="unknown",
        prepared="request",
        strict_codex_proof=True,
        dispatch_started_at=datetime(
            2026, 10, 30, 10, 0, 1, tzinfo=timezone.utc
        ).timestamp(),
        checkpoint={
            "path": str(path),
            "offset": stat.st_size,
            "backend": "codex",
            "inode": stat.st_ino,
            "device": stat.st_dev,
        },
    )
    recovery.records[request.id] = request
    replacement = tmp_path / "replacement"
    replacement.write_text(row("request", "2026-10-30T10:00:02Z"))
    replacement.replace(path)
    assert recovery.reconcile(request)


def test_legacy_request_without_dispatch_boundary_does_not_match_an_old_row(
    tmp_path, monkeypatch
):
    source = Session(
        id="s", name="s", window_id="@1", backend="codex", claude_session_id="native"
    )
    path = tmp_path / "native.jsonl"
    path.write_text(row("same", "2026-10-30T10:00:02Z"))
    monkeypatch.setattr(session_manager, "sessions", {"s": source})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native",
                backend="codex",
                cwd=str(tmp_path),
                transcript_path=str(path),
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal")
    request = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {"message": {"date": 1}},
        state="unknown",
        prepared="same",
        strict_codex_proof=True,
    )
    recovery.records[request.id] = request
    assert not recovery.reconcile(request)


@pytest.mark.asyncio
async def test_late_proof_corrects_warning_even_after_restart(tmp_path, monkeypatch):
    source = Session(
        id="s", name="s", window_id="@1", backend="codex", claude_session_id="native"
    )
    path = tmp_path / "native.jsonl"
    path.write_text(row("request", "2026-10-30T10:00:02Z"))
    monkeypatch.setattr(session_manager, "sessions", {"s": source})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native",
                backend="codex",
                cwd=str(tmp_path),
                transcript_path=str(path),
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal")
    request = InputRequest(
        "42:1",
        42,
        1,
        "s",
        "@1",
        "text",
        {},
        state="unknown",
        prepared="request",
        strict_codex_proof=True,
        notice_id=99,
        dispatch_started_at=datetime(
            2026, 10, 30, 10, 0, 1, tzinfo=timezone.utc
        ).timestamp(),
    )
    recovery.records[request.id] = request
    assert recovery.reconcile(request)
    restored = InputRecovery(recovery.path)
    restored.load()
    edit = AsyncMock()
    restored.bot = SimpleNamespace(edit_message_text=edit)
    task = asyncio.create_task(restored.feedback_loop())
    try:
        async with asyncio.timeout(1):
            while not edit.await_count:
                await asyncio.sleep(0)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert "подтверждено" in edit.await_args.kwargs["text"]
    assert not restored.resolved_notices
