from __future__ import annotations

import pytest

from ccbot.node_transport import EventSequence, NodeEnvelope, RequestReceiptLedger


def test_node_envelope_is_newline_delimited_json_and_round_trips() -> None:
    message = NodeEnvelope(
        kind="command",
        request_id="request-1",
        sequence=4,
        payload={"operation": "send_text", "text": "hello"},
    )

    restored = NodeEnvelope.from_json_line(message.to_json_line())

    assert restored == message


def test_receipt_ledger_deduplicates_retries() -> None:
    ledger = RequestReceiptLedger()

    first = ledger.accept("request-1")
    second = ledger.accept("request-1")
    completed = ledger.complete("request-1", {"status": "ok"})

    assert second == first
    assert completed.result == {"status": "ok"}
    assert ledger.accept("request-1") == completed


def test_event_sequence_ignores_replay_and_rejects_gap() -> None:
    cursor = EventSequence()
    first = NodeEnvelope(kind="event", sequence=1)
    replay = NodeEnvelope(kind="event", sequence=1)
    gap = NodeEnvelope(kind="event", sequence=3)

    assert cursor.accept(first) is True
    assert cursor.accept(replay) is False
    with pytest.raises(ValueError, match="event gap"):
        cursor.accept(gap)
