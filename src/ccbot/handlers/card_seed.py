"""Seed live-card state from an existing Claude or Codex transcript."""

from __future__ import annotations

import asyncio
import base64
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast


from ..session import Session, session_manager
from ..session_monitor import NewMessage
from ..transfer_runtime import get_node_runtime
from .card_model import (
    CARD_SEED_TURNS,
    CardState,
    Event,
    PendingPrompt,
    _apply_tool_result,
    _build_event,
    _strip_for_card,
)
from .card_seed_io import load_recent_parsed_entries

from .card_registry import _cards

logger = logging.getLogger(__name__)

SeedLoader = Callable[..., Awaitable[list[Event]]]


def _prompt_key(text: str) -> str:
    """Normalize only presentation differences when matching a JSONL echo."""
    return " ".join(_strip_for_card(text).split())


def matching_pending_prefix_count(
    pending_prompts: list[PendingPrompt], raw_text: str
) -> int:
    """Return the exact oldest pending prefix represented by one user row."""
    target = _prompt_key(raw_text)
    if not target:
        return 0
    compact = ""
    spaced_parts: list[str] = []
    matched = 0
    for index, pending in enumerate(pending_prompts, 1):
        part = _prompt_key(pending.text)
        if not part:
            break
        compact += part
        spaced_parts.append(part)
        if target in (compact, " ".join(spaced_parts)):
            matched = index
    return matched


def _reconcile_seeded_pending(state: CardState, seeded: list[Event]) -> int:
    """Bind pending receipts to their already-seeded user events in place.

    The seed loader can observe the user's JSONL row and the first assistant
    row in one read.  In that case the pending receipt must be consumed here;
    leaving it in ``pending_prompts`` renders the same request again as a
    synthetic tail after the assistant work that it initiated.
    """
    if not state.pending_prompts:
        return 0
    original_count = len(state.pending_prompts)

    # A queued Codex turn may contain several Telegram requests in one user
    # row. Reconcile exact multi-request batches first; the reverse single-row
    # matcher below then retains its protection against repeated old text.
    remaining = list(state.pending_prompts)
    for event in seeded:
        if event.type != "user_msg" or len(remaining) < 2:
            continue
        count = matching_pending_prefix_count(remaining, event.text)
        if count < 2 or event.started_at + 30.0 < remaining[0].created_at:
            continue
        consumed = remaining[:count]
        if any(pending.preprocessed for pending in consumed):
            event.user_icon = "👤💻"
        elif consumed[0].user_icon:
            event.user_icon = consumed[0].user_icon
        del remaining[:count]
    state.pending_prompts = remaining
    if not state.pending_prompts:
        return original_count

    matched_pending_ids: set[int] = set()
    search_before = len(seeded)
    for pending in reversed(state.pending_prompts):
        needle = _prompt_key(pending.text)
        if not needle:
            continue
        for index in range(search_before - 1, -1, -1):
            event = seeded[index]
            if event.type != "user_msg" or _prompt_key(event.text) != needle:
                continue
            if event.started_at + 30.0 < pending.created_at:
                # The same words in older history are not proof that the new
                # request reached the transcript. Keep its live receipt.
                continue
            # Match pending prompts from newest to oldest, preserving FIFO
            # order even when the same text was submitted more than once.
            if pending.preprocessed:
                event.user_icon = "👤💻"
            elif pending.user_icon:
                event.user_icon = pending.user_icon
            matched_pending_ids.add(id(pending))
            search_before = index
            break

    if matched_pending_ids:
        state.pending_prompts = [
            pending
            for pending in state.pending_prompts
            if id(pending) not in matched_pending_ids
        ]
    return original_count - len(state.pending_prompts)


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
    if sess.node_id != "local":
        seeded, _version = await _seed_remote_events(sess, max_turns, "")
        return seeded
    if not sess.window_id:
        return []
    # Derive the transcript path by pure path math instead of
    # ``resolve_session_for_window`` — the latter fully walks the JSONL
    # just to refresh summary/token stats we don't use here, then we read
    # the file again below. On a multi-MB resumed transcript that wasted
    # walk costs >1s. Same fast-path the history cache already uses.
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
    try:
        parsed_list = await asyncio.to_thread(load_recent_parsed_entries, fp, max_turns)
    except Exception as e:
        logger.debug("seed: tail read/parse failed for %s: %s", fp, e)
        return []

    return _events_from_parsed_entries(sess, parsed_list, max_turns)


def _entry_value(entry: Any, key: str, default: Any = None) -> Any:
    return (
        entry.get(key, default)
        if isinstance(entry, dict)
        else getattr(entry, key, default)
    )


def _entry_images(entry: Any) -> list[tuple[str, bytes]] | None:
    raw = _entry_value(entry, "image_data")
    if not raw:
        return None
    if not isinstance(entry, dict):
        return raw
    images: list[tuple[str, bytes]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            images.append(
                (
                    str(item.get("media_type", "application/octet-stream")),
                    base64.b64decode(str(item.get("data", "")), validate=True),
                )
            )
        except (ValueError, TypeError):
            continue
    return images or None


def _events_from_parsed_entries(
    sess: Session, parsed_list: list[Any], max_turns: int
) -> list[Event]:
    # Walk backwards collecting indices of end_turn boundaries (final
    # assistant text). Keep only entries from the last CARD_SEED_TURNS
    # boundaries - earlier history stays in JSONL for transcript recovery or
    # other history paths.
    end_turn_idxs: list[int] = []
    for i in range(len(parsed_list) - 1, -1, -1):
        p = parsed_list[i]
        if (
            _entry_value(p, "role", "") == "assistant"
            and _entry_value(p, "content_type", "") == "text"
            and _entry_value(p, "stop_reason", "")
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
        ct = _entry_value(p, "content_type", "text")
        msg = NewMessage(
            session_id="seed",
            text=_entry_value(p, "text", "") or "",
            is_complete=True,
            content_type=ct,
            tool_use_id=_entry_value(p, "tool_use_id"),
            role=_entry_value(p, "role", "assistant"),
            tool_name=_entry_value(p, "tool_name"),
            image_data=_entry_images(p),
            stop_reason=_entry_value(p, "stop_reason"),
            timestamp=_entry_value(p, "timestamp", "") or "",
            is_error=bool(_entry_value(p, "is_error", False)),
            api_error=str(_entry_value(p, "api_error", "") or ""),
        )
        ev = _build_event(msg)
        if ev.type == "user_msg" and sess.was_preprocessed_prompt(msg.text):
            ev.user_icon = "👤💻"
        if ct == "tool_result" and _apply_tool_result(pseudo_state, ev):
            continue
        events.append(ev)
    return events


async def _seed_remote_events(
    sess: Session, max_turns: int, known_version: str
) -> tuple[list[Event], str]:
    routing_id = sess.worker_session_id or sess.claude_session_id
    runtime = get_node_runtime(sess.node_id)
    if runtime is None or not routing_id:
        logger.info(
            "remote card seed deferred node=%s session=%s reason=offline",
            sess.node_id,
            sess.id,
        )
        return [], known_version
    try:
        result = await runtime.seed_session_history(
            sess.node_id,
            routing_id,
            max_turns,
            known_version=known_version,
        )
    except Exception as exc:
        logger.warning(
            "remote card seed failed node=%s session=%s error=%s",
            sess.node_id,
            sess.id,
            str(exc).strip() or type(exc).__name__,
        )
        return [], known_version
    version = str(result.get("version", known_version))
    if not result.get("ok", False):
        logger.warning(
            "remote card seed rejected node=%s session=%s error=%s",
            sess.node_id,
            sess.id,
            result.get("error", "unknown error"),
        )
        return [], version
    if result.get("unchanged"):
        return [], version
    raw_entries = result.get("entries", [])
    if not isinstance(raw_entries, list):
        logger.warning(
            "remote card seed invalid payload node=%s session=%s",
            sess.node_id,
            sess.id,
        )
        return [], version
    events = _events_from_parsed_entries(sess, raw_entries, max_turns)
    if not events:
        logger.info(
            "remote card seed empty node=%s session=%s version=%s",
            sess.node_id,
            sess.id,
            version,
        )
    return events, version


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
    # User-settable depth — Settings → Card history (10/20/50/100).
    try:
        max_turns = int(
            session_manager.get_user_settings(user_id).get(
                "card_history", CARD_SEED_TURNS
            )
        )
    except (TypeError, ValueError):
        max_turns = CARD_SEED_TURNS
    if sess.node_id != "local":
        seeded, version = await _seed_remote_events(
            sess, max_turns, state.remote_seed_version
        )
        state.remote_seed_version = version
    else:
        mtime = _transcript_mtime(sess)
        if mtime >= 0.0 and mtime == state.seed_mtime:
            # Nothing new on disk since the last empty attempt — skip the
            # re-parse and wait for the transcript to grow.
            return
        state.seed_mtime = mtime
        seeded = await _legacy_seed_loader()(sess, max_turns=max_turns)
    if seeded:
        reconciled = _reconcile_seeded_pending(state, seeded)
        for _ in range(min(reconciled, len(state.pending_request_sequences))):
            _message_id, sequence = state.pending_request_sequences.pop(0)
            state.active_turn_sequence = sequence
        # A live worker event may land while the bounded seed RPC is in flight.
        # Preserve it after the historical prefix and suppress an exact row
        # already present in the authoritative transcript response.
        signatures = {
            (event.type, event.text, event.body, event.tool_use_id, event.started_at)
            for event in seeded
        }
        live_tail = [
            event
            for event in state.events
            if (
                event.type,
                event.text,
                event.body,
                event.tool_use_id,
                event.started_at,
            )
            not in signatures
        ]
        state.events = [*seeded, *live_tail]
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
