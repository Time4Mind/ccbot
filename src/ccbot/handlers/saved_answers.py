"""Immutable answer pages; pagination never targets another session's live card."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)


def snapshot(item: Any) -> dict[str, Any] | None:
    from .card_final_delivery import restore_snapshot
    from .card_delivery_receipts import final_groups
    from .card_layout import _render_card
    from .card_pagination import paginate_events_for_card, _resolved_page_idx
    from ..session import Session, session_manager

    if not isinstance(item.payload.get("state"), dict):
        return None
    state = restore_snapshot(item)
    final = state.events[item.payload["final_index"]]
    group = next(
        g for g in final_groups(state.events) if any(e is final for e in g.events)
    )
    original_pages = paginate_events_for_card(state, item.user_id)
    visible = original_pages[_resolved_page_idx(state, len(original_pages))]
    frozen = replace(state, events=list(group.events), current_page_idx=0)
    pages = paginate_events_for_card(frozen, item.user_id)
    if len(pages) <= 1:
        return None
    sess = session_manager.get_session(item.session_id)
    # Keep the snapshot render independent of mutable session presentation.
    source = Session(
        id=item.session_id,
        name=getattr(sess, "name", item.session_id),
        workdir=getattr(sess, "workdir", ""),
    )
    current = next(
        (
            i
            for i, page in enumerate(pages)
            if any(e is v for e in page for v in visible)
        ),
        0,
    )
    rendered = []
    for i in range(len(pages)):
        frozen.current_page_idx = i
        rendered.append(_render_card(source, frozen, user_id=item.user_id))
    return {
        "pages": rendered,
        "page": current,
        "timestamp": final.started_at,
        "hash": hashlib.sha256(group.text.encode()).hexdigest()[:24],
    }


def store(
    queue: Any, user_id: int, message_id: int, answer: dict[str, Any]
) -> dict[str, Any]:
    """Write page content once, never rewrite it on pagination/checkpoints."""
    from ..utils import atomic_write_json

    path = (
        queue.path.parent
        / "final-answer-pages"
        / f"{user_id}-{message_id}-{answer['hash']}.json"
    )
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"pages": answer["pages"]}, indent=None)
    return {key: value for key, value in answer.items() if key != "pages"} | {
        "count": len(answer["pages"])
    }


def read_pages(queue: Any, user_id: int, message_id: int) -> list[str] | None:
    view = queue.answer_views.get((user_id, message_id))
    if not view:
        return None
    path = (
        queue.path.parent
        / "final-answer-pages"
        / f"{user_id}-{message_id}-{view['hash']}.json"
    )
    try:
        pages = json.loads(path.read_text())["pages"]
        return (
            pages
            if isinstance(pages, list) and all(isinstance(p, str) for p in pages)
            else None
        )
    except (OSError, ValueError, KeyError):
        return None


def keyboard(
    user_id: int, message_id: int, session_id: str, page: int | None = None
) -> InlineKeyboardMarkup:
    from ..keyboard_cleanup import cleanup_queue
    from .card_session_link import session_link_keyboard

    result = session_link_keyboard(user_id, session_id)
    cleanup_queue.load()
    view = cleanup_queue.answer_views.get((user_id, message_id))
    if (
        not view
        or cleanup_queue.final_sessions.get((user_id, message_id)) != session_id
    ):
        return result
    count = view["count"]
    index = view["page"] if page is None else page
    index = max(0, min(index, count - 1))
    row = [
        InlineKeyboardButton(
            label, callback_data=f"fa:{message_id}:{view['hash']}:{target}"
        )
        for label, target in (
            ("◀", max(0, index - 1)),
            (f"{index + 1}/{count}", index),
            ("▶", min(count - 1, index + 1)),
        )
    ]
    return InlineKeyboardMarkup([row, *result.inline_keyboard])


def observe_page(user_id: int, state: Any) -> None:
    """Track a live final's displayed page in RAM; invalidate a replaced answer."""
    from ..keyboard_cleanup import cleanup_queue
    from .card_delivery_receipts import final_groups
    from .card_pagination import paginate_events_for_card, _resolved_page_idx

    if state.msg_id is None:
        return
    cleanup_queue.load()
    key = user_id, state.msg_id
    view = cleanup_queue.answer_views.get(key)
    if not view or cleanup_queue.final_sessions.get(key) is None:
        return
    pages = paginate_events_for_card(state, user_id)
    selected = _resolved_page_idx(state, len(pages))
    group = next(
        (
            g
            for g in final_groups(state.events)
            if g.events[0].started_at == view["timestamp"]
            and hashlib.sha256(g.text.encode()).hexdigest()[:24] == view["hash"]
        ),
        None,
    )
    ids = {id(e) for e in group.events} if group else set()
    matching_pages = [i for i, p in enumerate(pages) if any(id(e) in ids for e in p)]
    if selected in matching_pages:
        view["page"] = min(matching_pages.index(selected), view["count"] - 1)
    else:
        # Same-session repaint can replace the answer just like a carrier handoff.
        cleanup_queue.final_sessions[key] = None
        cleanup_queue.save()


def discard(queue: Any, user_id: int, message_id: int, view: dict[str, Any]) -> None:
    """Bound the derived payload cache to the retained ownership receipts."""
    path = (
        queue.path.parent
        / "final-answer-pages"
        / f"{user_id}-{message_id}-{view['hash']}.json"
    )
    path.unlink(missing_ok=True)
