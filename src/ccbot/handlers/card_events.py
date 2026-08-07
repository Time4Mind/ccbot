"""Convert monitor messages into card events and fold tool results."""

from __future__ import annotations

from ..session_monitor import NewMessage
from .card_event_render import _build_tool_spoiler_body
from .card_text import (
    _extract_expquote_inner,
    _parse_timestamp,
    _split_tool_text,
    _strip_for_card,
    _trim,
)
from .card_types import CardState, Event

__all__ = [
    "_build_event",
    "_apply_tool_result",
]


def _build_event(msg: NewMessage) -> Event:
    """Build an ``Event`` from one ``NewMessage``.

    ``tool_result`` is the only special case: callers should NOT append
    the returned Event to the card. Instead they look up the matching
    ``tool_use`` Event by ``tool_use_id`` and fold the result in via
    ``_apply_tool_result``. We still build a placeholder Event so
    callers that find no match (race / restart) can fall back to
    appending it.
    """
    text = _strip_for_card(msg.text or "")
    raw_body = msg.text or ""
    started = _parse_timestamp(msg.timestamp)

    if msg.content_type == "thinking":
        # Thinking text reaches us already wrapped in EXPQUOTE sentinels
        # (transcript_parser → format_expandable_quote). For the card we
        # render as plain indented text — pull the inner content out so
        # ``_indent_body`` doesn't strip it away as a quote block.
        inner = _extract_expquote_inner(raw_body)
        body_text = inner if inner else ""
        # The placeholder ``(thinking)`` is parser fallback when there's
        # no thinking_text — show only the head, no duplicated body row.
        if body_text.strip() == "(thinking)":
            body_text = ""
        return Event(
            type="thinking",
            text="",  # head is just "∴ thinking" — no per-event preview text
            body=body_text,
            started_at=started,
        )
    if msg.content_type == "tool_use":
        name, args, _summary, content = _split_tool_text(raw_body)
        # Card head shows ONLY the tool name (e.g. "Bash" / "Read") —
        # args / content go under the spoiler. Bash args get a fenced
        # ``bash`` block, other tools' args land in an inline ``code``
        # span; Read/Write content picks a language from the file
        # extension; Edit content goes through ``diff``.
        spoiler_body = _build_tool_spoiler_body(name, args, content)
        return Event(
            type="tool_use",
            text=_trim(name, 80),
            body=spoiler_body,
            started_at=started,
            tool_use_id=msg.tool_use_id,
            tool_name=msg.tool_name,
        )
    if msg.content_type == "tool_result":
        name, args, summary, content = _split_tool_text(raw_body)
        # Head: just the tool name + summary inline (e.g. "Edit · Added
        # 12 lines"). args / content go through the same syntax-
        # highlight pipeline as tool_use.
        head_with_summary = (
            f"{name} · {summary}" if (name and summary) else name or summary
        )
        spoiler_body = _build_tool_spoiler_body(name, args, content)
        return Event(
            type="tool_result",
            text=_trim(head_with_summary, 120),
            body=spoiler_body,
            started_at=started,
            tool_use_id=msg.tool_use_id,
            image_data=msg.image_data,
            is_error=msg.is_error,
        )
    if msg.role == "user":
        return Event(
            type="user_msg",
            text=_trim(text, 200),
            body=raw_body,
            started_at=started,
        )
    is_final = msg.stop_reason in ("end_turn", "stop_sequence", "max_tokens")
    # Narrative text events (mid-stream chunks and final answers) render
    # ``event.text`` verbatim — don't ``_trim`` them, that would clip the
    # answer at 200 chars and flatten newlines. The 200-char ``_trim`` cap
    # is only meaningful for one-line summary heads (tool_use / thinking /
    # user_msg).
    return Event(
        type="final_text" if is_final else "text",
        text=text,
        body=raw_body,
        started_at=started,
        completed_at=started if is_final else None,
        is_page_break=is_final,
    )


def _apply_tool_result(state: CardState, result: Event) -> bool:
    """Fold a ``tool_result`` Event into the matching ``tool_use``.

    Mutates the tool_use Event in place: ``completed_at``, ``body`` and
    ``is_error`` are updated; image_data is carried over for the send
    path. Returns True on success, False when no match found (caller
    should append ``result`` as-is).
    """
    if not result.tool_use_id:
        return False
    for ev in reversed(state.events):
        if ev.type == "tool_use" and ev.tool_use_id == result.tool_use_id:
            ev.completed_at = result.started_at
            ev.body = result.body or ev.body
            ev.text = result.text or ev.text
            ev.is_error = result.is_error
            ev.image_data = result.image_data
            return True
    return False
