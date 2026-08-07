"""Seed live-card state from an existing Claude or Codex transcript."""

from __future__ import annotations

from __future__ import annotations

import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import cast


from ..session import Session, session_manager
from ..session_monitor import NewMessage
from .card_model import (
    CARD_SEED_TURNS,
    CardState,
    Event,
    _apply_tool_result,
    _build_event,
)

from .card_registry import _cards

logger = logging.getLogger(__name__)

SeedLoader = Callable[..., Awaitable[list[Event]]]


__all__ = [
    "get_card_state",
    "_seed_events_from_jsonl",
    "_transcript_mtime",
    "_ensure_seeded",
]


def get_card_state(user_id: int, sess: Session) -> CardState:
    return _cards.setdefault((user_id, sess.id), CardState())


async def _seed_events_from_jsonl(
    sess: Session, max_turns: int = CARD_SEED_TURNS
) -> list[Event]:
    """Build a list[Event] from the session's JSONL transcript.

    Pulls the last ``max_turns`` end-of-turn boundaries so the card has
    visible history after a bot restart (when in-memory ``state.events``
    is empty). Returns ``[]`` on any failure — caller just continues
    with an empty card.

    ``max_turns`` defaults to the module constant but is overridden by
    ``_ensure_seeded`` from the user's ``card_history`` setting.
    """
    if not sess.window_id:
        return []
    # Derive the transcript path by pure path math instead of
    # ``resolve_session_for_window`` — the latter fully walks the JSONL
    # just to refresh summary/token stats we don't use here, then we read
    # the file again below. On a multi-MB resumed transcript that wasted
    # walk costs >1s. Same fast-path the /history cache already uses.
    state = session_manager.get_window_state(sess.window_id)
    if not state.session_id or not state.cwd:
        return []
    if state.transcript_path:
        fp = Path(state.transcript_path)
    elif sess.backend == "codex":
        from ..codex_session_io import build_session_file_path

        fp = build_session_file_path(state.session_id, state.cwd)
    else:
        from ..session_claude_io import build_session_file_path

        fp = build_session_file_path(state.session_id, state.cwd)
    if fp is None or not fp.exists():
        return []
    file_path = str(fp)
    import json as _json
    from pathlib import Path as _Path

    from ..transcript_parser import TranscriptParser

    try:
        raw = _Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        logger.debug("seed: read JSONL %s failed: %s", file_path, e)
        return []
    raw_entries: list[dict[str, object]] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            raw_entries.append(_json.loads(line))
        except Exception:
            continue
    try:
        parsed_list, _ = TranscriptParser.parse_entries(raw_entries, pending_tools=None)
    except Exception as e:
        logger.debug("seed: parse_entries failed: %s", e)
        return []

    # Walk backwards collecting indices of end_turn boundaries (final
    # assistant text). Keep only entries from the last CARD_SEED_TURNS
    # boundaries — earlier history stays in JSONL for /screenshot or
    # other history paths.
    end_turn_idxs: list[int] = []
    for i in range(len(parsed_list) - 1, -1, -1):
        p = parsed_list[i]
        if (
            getattr(p, "role", "") == "assistant"
            and getattr(p, "content_type", "") == "text"
            and getattr(p, "stop_reason", "")
            in ("end_turn", "stop_sequence", "max_tokens")
        ):
            end_turn_idxs.append(i)
            if len(end_turn_idxs) >= max_turns:
                break
    if end_turn_idxs:
        start_idx = end_turn_idxs[-1]
        # Pull a few entries back from start_idx so the user message that
        # triggered the oldest kept turn is visible at the top.
        start_idx = max(0, start_idx - 4)
    else:
        start_idx = max(0, len(parsed_list) - 80)
    tail = parsed_list[start_idx:]

    # Convert ParsedEntry → NewMessage → Event. tool_results fold into
    # matching tool_use via _apply_tool_result; on miss they append.
    pseudo_state = CardState()
    events = pseudo_state.events
    for p in tail:
        ct = getattr(p, "content_type", "text")
        msg = NewMessage(
            session_id="seed",
            text=getattr(p, "text", "") or "",
            is_complete=True,
            content_type=ct,
            tool_use_id=getattr(p, "tool_use_id", None),
            role=getattr(p, "role", "assistant"),
            tool_name=getattr(p, "tool_name", None),
            image_data=getattr(p, "image_data", None),
            stop_reason=getattr(p, "stop_reason", None),
            timestamp=getattr(p, "timestamp", "") or "",
        )
        ev = _build_event(msg)
        if ct == "tool_result" and _apply_tool_result(pseudo_state, ev):
            continue
        events.append(ev)
    return events


def _legacy_seed_loader() -> SeedLoader:
    """Resolve the notifications facade's monkeypatchable seed loader."""
    facade = sys.modules.get(f"{__package__}.notifications")
    if facade is None:
        return _seed_events_from_jsonl
    candidate = getattr(facade, "_seed_events_from_jsonl", _seed_events_from_jsonl)
    return cast(SeedLoader, candidate)


def _transcript_mtime(sess: Session) -> float:
    """Return the mtime (epoch seconds) of the session's JSONL transcript,
    or -1.0 if the path can't be resolved / the file is missing.

    Cheap (single ``stat``) — used by ``_ensure_seeded`` to gate empty-seed
    retries on a restored session without re-parsing the whole transcript.
    """
    if not sess.window_id:
        return -1.0
    state = session_manager.get_window_state(sess.window_id)
    if not state.session_id or not state.cwd:
        return -1.0
    if state.transcript_path:
        fp = Path(state.transcript_path)
    elif sess.backend == "codex":
        from ..codex_session_io import build_session_file_path

        fp = build_session_file_path(state.session_id, state.cwd)
    else:
        from ..session_claude_io import build_session_file_path

        fp = build_session_file_path(state.session_id, state.cwd)
    if fp is None:
        return -1.0
    try:
        return fp.stat().st_mtime
    except OSError:
        return -1.0


async def _ensure_seeded(user_id: int, sess: Session, state: CardState) -> None:
    """Seed ``state.events`` from JSONL on first access after restart.

    No-op when events already exist. Latches ``seed_attempted`` only on a
    *successful* (non-empty) seed: a freshly restored (``claude --resume``)
    session builds its card before claude has flushed the resumed transcript
    to disk, so an early read returns [] — latching then would block the
    seed forever and the history would never reach the card. An empty read
    instead leaves the flag clear and retries on a later event, gated on the
    transcript mtime advancing (``state.seed_mtime``) so a burst of events
    during the resume window doesn't re-parse a multi-MB JSONL each time. A
    wipe site that wants a re-seed clears ``seed_attempted`` + ``seed_mtime``
    (see ``CardState.seed_attempted``).
    """
    if state.events:
        return
    if state.seed_attempted:
        return
    mtime = _transcript_mtime(sess)
    if mtime >= 0.0 and mtime == state.seed_mtime:
        # Nothing new on disk since the last empty attempt — skip the
        # re-parse and wait for the transcript to grow.
        return
    state.seed_mtime = mtime
    # User-settable depth — Settings → Card history (10/20/50/100).
    try:
        max_turns = int(
            session_manager.get_user_settings(user_id).get(
                "card_history", CARD_SEED_TURNS
            )
        )
    except (TypeError, ValueError):
        max_turns = CARD_SEED_TURNS
    seeded = await _legacy_seed_loader()(sess, max_turns=max_turns)
    if seeded:
        state.events = seeded
        state.seed_attempted = True
        logger.info(
            "card_seeded user=%d sess=%s events=%d",
            user_id,
            sess.id,
            len(seeded),
            extra={
                "event": "card_seeded",
                "user_id": user_id,
                "session_id": sess.id,
                "events": len(seeded),
            },
        )
