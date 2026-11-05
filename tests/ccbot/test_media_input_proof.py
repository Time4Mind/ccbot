"""Native image rows prove media delivery without replay or loose matching."""

import json

import pytest

from ccbot.input_recovery import InputRecovery, InputRequest
from ccbot.session import Session, WindowState, session_manager


def native_row(path, caption="", *, label="[Image #1]", second=2):
    content = [
        {"type": "input_text", "text": f'<image name={label} path="{path}">'},
        {"type": "input_image", "image_url": "data:image/png;base64,cHJvYmU="},
        {"type": "input_text", "text": "</image>"},
        {"type": "input_text", "text": label},
    ]
    if caption:
        content.append({"type": "input_text", "text": caption})
    return {
        "timestamp": f"2026-11-05T06:04:{second:02d}Z",
        "type": "response_item",
        "payload": {"type": "message", "role": "user", "content": content},
    }


@pytest.fixture
def proof(tmp_path, monkeypatch):
    transcript = tmp_path / "native.jsonl"
    transcript.write_text("")
    session = Session(
        id="source",
        name="source",
        window_id="@1",
        backend="codex",
        claude_session_id="native",
    )
    monkeypatch.setattr(session_manager, "sessions", {session.id: session})
    monkeypatch.setattr(
        session_manager,
        "window_states",
        {
            "@1": WindowState(
                session_id="native", backend="codex", transcript_path=str(transcript)
            )
        },
    )
    recovery = InputRecovery(tmp_path / "journal.json")

    def make(number=1, caption="", kind="photo"):
        relative = ".ccbot-inbox/probe.jpg"
        request = InputRequest(
            f"42:{number}",
            42,
            number,
            session.id,
            "@1",
            kind,
            {},
            state="unknown",
            strict_codex_proof=True,
            prepared=f"{caption}\n\n{relative}" if caption else relative,
            media_path=str(tmp_path / ".ccbot-inbox/probe.jpg"),
            checkpoint={"path": str(transcript), "offset": 0, "backend": "codex"},
        )
        recovery.records[request.id] = request
        return request

    def append(row):
        with transcript.open("a") as stream:
            stream.write(json.dumps(row) + "\n")

    return recovery, make, append, transcript


@pytest.mark.parametrize("kind", ["photo", "document"])
@pytest.mark.parametrize("caption", ["", "Проверь отчёт"])
def test_native_image_confirms_media_and_resolves_false_notice(proof, kind, caption):
    recovery, make, append, _ = proof
    request = make(caption=caption, kind=kind)
    request.notice_id = 77
    append(native_row(".ccbot-inbox/probe.jpg", caption))
    assert recovery.reconcile(request)
    assert request.confirmed and request.id not in recovery.records
    assert len(recovery.resolved_notices) == 1
    assert recovery.resolved_notices[0]["notice_id"] == 77
    assert recovery.resolved_notices[0]["message_id"] == 1


@pytest.mark.parametrize(
    "mismatch",
    [
        "path",
        "foreign_parent",
        "caption",
        "no_image",
        "no_close",
        "wrong_label",
        "unwrapped",
        "extra_image",
        "two_images",
        "missing_identity",
    ],
)
def test_native_image_requires_exact_attachment_and_complete_structure(proof, mismatch):
    recovery, make, append, _ = proof
    request = make(caption="Проверь отчёт")
    value = native_row(".ccbot-inbox/probe.jpg", "Проверь отчёт")
    content = value["payload"]["content"]
    if mismatch in ("path", "foreign_parent"):
        different = (
            ".ccbot-inbox/other.jpg" if mismatch == "path" else "/elsewhere/probe.jpg"
        )
        content[0]["text"] = f'<image name=[Image #1] path="{different}">'
    elif mismatch == "caption":
        content[-1]["text"] = "Проверь другую таблицу"
    elif mismatch == "no_image":
        content.pop(1)
    elif mismatch == "no_close":
        content.pop(2)
    elif mismatch == "wrong_label":
        content[3]["text"] = "[Image #2]"
    elif mismatch == "unwrapped":
        content[:] = [
            {"type": "input_text", "text": request.prepared},
            {"type": "input_image", "image_url": "opaque"},
        ]
    elif mismatch == "extra_image":
        content.append({"type": "input_image", "image_url": "opaque"})
    elif mismatch == "two_images":
        content.extend(native_row(".ccbot-inbox/probe.jpg")["payload"]["content"])
    else:
        request.media_path = ""
    append(value)
    assert not recovery.reconcile(request)
    assert request.id in recovery.records and not request.confirmed
    assert not recovery.resolved_notices


def test_one_native_image_row_cannot_confirm_two_requests_after_restart(proof):
    recovery, make, append, _ = proof
    first, second = make(1), make(2)
    append(native_row(".ccbot-inbox/probe.jpg"))
    assert recovery.reconcile(first)
    restarted = InputRecovery(recovery.path)
    restarted.load()
    assert not restarted.reconcile(restarted.records[second.id])
    append(native_row(".ccbot-inbox/probe.jpg", second=3))
    assert restarted.reconcile(restarted.records[second.id])


@pytest.mark.parametrize("boundary", ["offset", "timestamp"])
def test_historical_native_image_does_not_ack_new_request(proof, boundary):
    from datetime import datetime, timezone

    recovery, make, append, transcript = proof
    append(native_row(".ccbot-inbox/probe.jpg"))
    request = make()
    if boundary == "offset":
        request.checkpoint["offset"] = transcript.stat().st_size
    else:
        request.checkpoint["not_before"] = datetime(
            2026, 11, 5, 6, 4, 3, tzinfo=timezone.utc
        ).timestamp()
    assert not recovery.reconcile(request)
    append(native_row(".ccbot-inbox/probe.jpg", second=4))
    assert recovery.reconcile(request)


def test_plain_document_path_still_confirms(proof):
    recovery, make, append, _ = proof
    request = make(kind="document")
    value = native_row(".ccbot-inbox/probe.jpg")
    value["payload"]["content"] = [{"type": "input_text", "text": request.prepared}]
    append(value)
    assert recovery.reconcile(request)
