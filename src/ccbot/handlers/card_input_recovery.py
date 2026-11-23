"""Project durable Telegram delivery failures onto the live transcript."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..media_gap import strip_media_gap_prefix
from .card_types import CardState, Event

if TYPE_CHECKING:
    from ..input_recovery import InputRecovery


@dataclass
class RecoveryRow:
    message_id: int
    anchor: Event
    tail: str
    sent_at: float
    settled_at: float


def recovery_rows(
    recovery: InputRecovery, user_id: int, session_id: str
) -> list[RecoveryRow]:
    rows = {}
    for key, entry in recovery.retry_sources.items():
        source = entry["source"]
        if (source["user_id"], source["session_id"]) == (user_id, session_id):
            rows[key] = (
                source,
                entry["status"],
                entry.get("sent_at", 0),
                entry.get("settled_at", 0),
            )
    for req in recovery.records.values():
        if (req.user_id, req.session_id) != (user_id, session_id):
            continue
        if req.state in ("unknown", "failed") and not (
            req.id in rows and rows[req.id][1] == "dismissed"
        ):
            rows[req.id] = (vars(req), req.state, req.dispatch_started_at, 0)
    result = []
    for source, status, sent_at, settled_at in rows.values():
        if status == "dismissed":
            continue
        message = source["update"].get("message", {})
        number = source["message_id"]
        caption = strip_media_gap_prefix(
            source["prepared"]
            or source["recognized"]
            or message.get("text")
            or message.get("caption")
            or ""
        )
        received = source["received_at"] or message.get("date", 0)
        tail = ""
        label = {"voice": "Голосовое", "document": "Документ", "photo": "Фото"}.get(
            source["kind"], "Запрос"
        )
        if status == "confirmed":
            text = "👤 " + (caption or f"{label} №{number}")
        else:
            reason = {
                "retrying": "повтор ожидает подтверждения",
                "failed": "не отправлен",
            }.get(status, "получение моделью не подтверждено")
            tail = f"⚠ Запрос №{number} · {reason}\n/requests_retry_{number}"
            text = (f"{label}: {caption[:320]}\n\n" if caption else "") + tail
        result.append(
            RecoveryRow(
                number,
                Event(
                    "input_recovery", text, received, body=caption, is_page_break=True
                ),
                tail,
                sent_at,
                settled_at,
            )
        )
    return sorted(result, key=lambda row: (row.anchor.started_at, row.message_id))


def refresh_rows(
    state: CardState, recovery: InputRecovery, user_id: int, session_id: str
) -> None:
    rows = recovery_rows(recovery, user_id, session_id)
    if rows != state.recovery_rows:
        state.recovery_rows = rows
        state.pagination_prefix_len = -1
    state.recovery_rows_loaded = True


def with_recovery_rows(events: list[Event], rows: list[RecoveryRow]) -> list[Event]:
    if not rows:
        return events
    # Keep native order. A delivered echo substitutes for its synthetic receipt
    # at the original Telegram position, including when a retry was much later.
    merged = list(events)
    for row in rows:
        anchor = row.anchor
        if not row.tail:
            for i, event in enumerate(merged):
                if (
                    event.type == "user_msg"
                    and row.sent_at - 1 <= event.started_at <= row.settled_at + 1
                    and strip_media_gap_prefix(event.body or event.text).strip()
                    == anchor.body.strip()
                ):
                    merged.pop(i)
                    break
        index = bisect_right(
            merged, anchor.started_at, key=lambda event: event.started_at
        )
        merged.insert(index, anchor)
    return merged
