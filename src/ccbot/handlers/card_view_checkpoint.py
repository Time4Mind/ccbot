"""Small view anchors piggyback on the existing session-state checkpoints."""

from __future__ import annotations

import hashlib
from typing import Any

from .card_pagination import (
    _rechunk_oversized_finals_inplace,
    _resolve_line_budget,
    _resolved_page_idx,
    paginate_events_for_card,
)
from .card_types import CardState, Event


def _identity(event: Event) -> str:
    return hashlib.sha256(event.text.encode()).hexdigest()[:24]


def checkpoint_views(manager: Any) -> dict[str, dict[str, Any]]:
    """No I/O or timers: called only from the existing save_state path."""
    from .card_registry import _cards

    result = dict(manager.card_views)
    for user_id, session_id in manager.active_sessions.items():
        state = _cards.get((user_id, session_id))
        if state is None or state.msg_id != manager.card_msg_id.get(user_id):
            continue
        key = str(user_id)
        # Menus are transient; retain the underlying reading position.
        if state.current_page_idx is None or not state.events:
            result.pop(key, None)
            continue
        pages = paginate_events_for_card(state, user_id)
        index = _resolved_page_idx(state, len(pages))
        if not pages[index]:
            continue
        event = pages[index][0]
        related = [
            i
            for i, page in enumerate(pages)
            if any(
                e.type == event.type and e.started_at == event.started_at for e in page
            )
        ]
        result[key] = {
            "session_id": session_id,
            "message_id": state.msg_id,
            "type": event.type,
            "timestamp": event.started_at,
            "hash": _identity(event),
            "part": related.index(index),
        }
    return {
        uid: view
        for uid, view in result.items()
        if manager.active_sessions.get(int(uid)) == view.get("session_id")
    }


def restore_view(manager: Any, user_id: int, session_id: str, state: CardState) -> None:
    """Resolve content identity, never a stale absolute transcript-page index."""
    view = manager.card_views.get(str(user_id), {})
    if view.get("session_id") != session_id or view.get("message_id") != state.msg_id:
        return
    _rechunk_oversized_finals_inplace(state, _resolve_line_budget(user_id))
    pages = paginate_events_for_card(state, user_id)
    related = []
    for i, page in enumerate(pages):
        matches = [
            e
            for e in page
            if e.type == view.get("type") and e.started_at == view.get("timestamp")
        ]
        if matches:
            related.append(i)
        if any(_identity(e) == view.get("hash") for e in matches):
            state.current_page_idx = i
            return
    # Page-size changes may split the same answer differently.
    part = view.get("part")
    if related and isinstance(part, int) and part >= 0:
        state.current_page_idx = related[min(part, len(related) - 1)]
