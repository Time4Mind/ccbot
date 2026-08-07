"""Paginate card events and enforce per-user line and Telegram byte budgets."""

from __future__ import annotations

import time

from ..session import session_manager
from .card_budget import (
    _chunk_final_text,
    _count_lines,
    _estimate_md_v2_size,
    _is_in_flight,
)
from .card_event_render import render_event
from .card_types import (
    CARD_PAGE_BUDGET,
    CARD_PAGE_LINES_DEFAULT,
    CARD_PAGE_LINES_OVERSHOOT,
    STALE_CARD_SECONDS,
    CardState,
    Event,
)

__all__ = [
    "paginate_events",
    "_EVENT_JOINER",
    "_JOINER_LINES",
    "_split_page_by_budget",
    "paginate_events_for_card",
    "_resolved_page_idx",
    "render_page",
    "_rechunk_oversized_finals_inplace",
    "_resolve_line_budget",
    "_trim_page_events",
    "card_page_info",
    "_is_stale",
    "_duplicate_of_seeded",
    "_card_is_busy",
    "_latest_inflight_idx",
]


def paginate_events(events: list[Event]) -> list[list[Event]]:
    """Split ``events`` into pages by ``is_page_break``.

    Page break: each Event with ``is_page_break=True`` becomes the TOP
    of a new page (everything before it lives on the previous page).
    Empty input → ``[[]]`` so callers can address page 0.

    NOTE: this is the "logical" pagination — by answer boundary only.
    Live cards must use :func:`paginate_events_for_card` to also split
    over-budget logical pages into navigable sub-pages, so the ◀/▶
    counter matches what's actually rendered.
    """
    pages: list[list[Event]] = []
    current: list[Event] = []
    for ev in events:
        if ev.is_page_break and current:
            pages.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        pages.append(current)
    return pages if pages else [[]]


# Inter-event joiner — sandwiches a non-breaking-space paragraph
# between events so CommonMark/MarkdownV2 render a TWO-paragraph gap
# (a single blank line is what consecutive ``\n\n\n`` collapsed to,
# which the user found too tight between thinking / tool / text blocks).
# Using `` `` (instead of HTML ``<br>``) keeps the gap consistent
# across the rich-message path AND the MarkdownV2 fallback — ``<br>``
# isn't in ``_html_inline_to_markdown``'s whitelist so it would leak
# as a literal ``<br>`` to chat in the fallback path.
_EVENT_JOINER = "\n\n \n\n"
# Account for the joiner when summing per-event line counts in
# sub-pagination — ``_EVENT_JOINER`` contains 4 ``\n`` chars + 1
# whitespace char, which adds 3 logical lines between any two events.
_JOINER_LINES = 3


def _split_page_by_budget(page: list[Event], budget_lines: int) -> list[list[Event]]:
    """Split one logical page into budget-fitting sub-pages.

    Returns the page unchanged when it fits in BOTH ``budget_lines +
    CARD_PAGE_LINES_OVERSHOOT`` AND ``CARD_PAGE_BUDGET`` bytes (the
    MD-V2-rendered byte cap Telegram enforces at edit time). Otherwise
    greedy-packs events forward: flush to a new sub-page when adding
    the next event (plus joiner overhead) would push us past either
    budget.

    Without the byte check, a page with many small events (e.g. a
    chain of single-line tool_use rows with MD-V2-escape-heavy paths)
    can pass the line budget but still produce a >4096-byte rendered
    body — Telegram refuses the edit with ``Message_too_long``, the
    card body stops rendering for that page (observed on tests/@120).

    A single huge event (one tool_result that alone exceeds budget)
    lands on its own sub-page — we don't split events, EXPQUOTE
    sentinels must stay paired.

    Sub-pages are navigable via ◀/▶: the user lands on the LATEST
    sub-page (default focus) and can step back to read older events.
    """
    if not page:
        return [page]
    now = time.time()
    cap = budget_lines + CARD_PAGE_LINES_OVERSHOOT
    rendered = render_page(page, now=now)
    if (
        _count_lines(rendered) <= cap
        and _estimate_md_v2_size(rendered) <= CARD_PAGE_BUDGET
    ):
        return [page]
    sub_pages: list[list[Event]] = []
    current: list[Event] = []
    current_lines = 0
    current_bytes = 0
    # Joiner byte cost = 4 ``\n`` (1 byte each) + 1 `` `` (2 bytes
    # UTF-8). MD-V2 escape doesn't touch any of these so the
    # post-conversion size matches the source.
    _JOINER_BYTES = len(_EVENT_JOINER.encode("utf-8"))
    for ev in page:
        rendered_ev = render_event(ev, in_flight=False, now=now)
        ev_lines = _count_lines(rendered_ev)
        ev_bytes = _estimate_md_v2_size(rendered_ev)
        line_overhead = _JOINER_LINES if current else 0
        byte_overhead = _JOINER_BYTES if current else 0
        line_overflow = current_lines + line_overhead + ev_lines > budget_lines
        byte_overflow = current_bytes + byte_overhead + ev_bytes > CARD_PAGE_BUDGET
        if current and (line_overflow or byte_overflow):
            sub_pages.append(current)
            current = [ev]
            current_lines = ev_lines
            current_bytes = ev_bytes
        else:
            current.append(ev)
            current_lines += line_overhead + ev_lines
            current_bytes += byte_overhead + ev_bytes
    if current:
        sub_pages.append(current)
    return sub_pages


def paginate_events_for_card(
    state: CardState, user_id: int | None
) -> list[list[Event]]:
    """Canonical pagination for live cards (is_page_break + budget split).

    The ◀/▶ counter and the rendered body MUST agree. Older callers
    that used :func:`paginate_events` directly would report 1/1 while
    the body silently dropped middle events ("(+N older events on
    previous pages)"). This unified entry point makes both sides see
    the same page list.
    """
    budget = _resolve_line_budget(user_id)
    base_pages = paginate_events(state.events)
    final_pages: list[list[Event]] = []
    for page in base_pages:
        final_pages.extend(_split_page_by_budget(page, budget))
    return final_pages or [[]]


def _resolved_page_idx(state: CardState, total_pages: int) -> int:
    """``current_page_idx`` clamped, with ``None`` → last (default focus)."""
    if total_pages <= 0:
        return 0
    if state.current_page_idx is None:
        return total_pages - 1
    return max(0, min(state.current_page_idx, total_pages - 1))


def render_page(events: list[Event], now: float) -> str:
    """Render the events of one page into a single body string.

    Events are joined by ``_EVENT_JOINER`` — a non-breaking-space
    paragraph wedged between two paragraph breaks. CommonMark / Telegram
    rich would otherwise collapse two consecutive blank rows into a
    single one, but a paragraph that contains a ``\\u00a0`` survives
    trimming and gives the user a visibly larger gap between thinking,
    tool_use and tool_result blocks.
    """
    parts: list[str] = []
    for i, ev in enumerate(events):
        parts.append(render_event(ev, in_flight=_is_in_flight(ev, events, i), now=now))
    return _EVENT_JOINER.join(parts)


def _rechunk_oversized_finals_inplace(state: CardState, budget_lines: int) -> None:
    """Walk ``state.events`` and split oversized ``final_text`` Events.

    Idempotent: an Event already fitting BOTH ``budget_lines`` AND the
    MarkdownV2-rendered byte budget (``CARD_PAGE_BUDGET``) is left
    untouched. An oversized Event is replaced (in place, preserving
    order) by N ``final_text`` Events produced by ``_chunk_final_text``,
    each marked ``is_page_break=True`` so pagination treats every chunk
    as a separate page.

    The byte gate matters: a wide single-paragraph answer can fit in
    ``cap`` visual lines and STILL produce a >4096-byte payload after
    MarkdownV2 escaping → ``Message_too_long`` on edit, plain-text
    fallback and repost all fail → the live card freezes on the previous
    body and the user never sees the reply. Splitting on rendered size
    keeps every chunk within Telegram's edit limit.
    """
    cap_lines = budget_lines + CARD_PAGE_LINES_OVERSHOOT
    i = 0
    while i < len(state.events):
        ev = state.events[i]
        if ev.type != "final_text" or not ev.text:
            i += 1
            continue
        fits_lines = _count_lines(ev.text) <= cap_lines
        fits_bytes = _estimate_md_v2_size(ev.text) <= CARD_PAGE_BUDGET
        if fits_lines and fits_bytes:
            i += 1
            continue
        chunks = _chunk_final_text(ev.text, budget_lines, CARD_PAGE_BUDGET)
        if len(chunks) <= 1:
            # _chunk_final_text refused to split (e.g. one huge unbroken
            # token with no boundary candidates). Leave as is.
            i += 1
            continue
        replacement = [
            Event(
                type="final_text",
                text=chunk,
                body=chunk,
                started_at=ev.started_at,
                completed_at=ev.completed_at,
                is_page_break=True,
            )
            for chunk in chunks
        ]
        state.events[i : i + 1] = replacement
        i += len(replacement)


def _resolve_line_budget(user_id: int | None) -> int:
    """Read the user's ``card_page_lines`` setting (15/30/50/100).

    Returns the default when the user has no setting or ``user_id`` is
    None (e.g. unit-test paths). Always clamps to the allowed range.
    """
    if user_id is None:
        return CARD_PAGE_LINES_DEFAULT
    try:
        raw = session_manager.get_user_settings(user_id).get(
            "card_page_lines", CARD_PAGE_LINES_DEFAULT
        )
        value = int(raw)
    except (TypeError, ValueError):
        value = CARD_PAGE_LINES_DEFAULT
    if value not in (10, 20, 40, 70):
        return CARD_PAGE_LINES_DEFAULT
    return value


def _trim_page_events(events: list[Event], budget_lines: int) -> list[Event]:
    """Drop middle events from ``events`` until rendered line-count
    ≤ ``budget_lines`` (with ``CARD_PAGE_LINES_OVERSHOOT`` slack).

    Always preserves:
    * The FIRST event (page anchor — usually the ``is_page_break``
      final_text answer; user needs the answer at the top of the page).
    * The TAIL events that fit in remaining budget (latest signal —
      in-flight tool, last narration).

    Middle events drop first. Whole-event boundaries only so EXPQUOTE
    sentinels stay paired.
    """
    if not events:
        return events
    now = time.time()
    full_lines = _count_lines(render_page(events, now=now))
    cap = budget_lines + CARD_PAGE_LINES_OVERSHOOT
    if full_lines <= cap:
        return events
    anchor = events[0]
    anchor_lines = _count_lines(render_event(anchor, in_flight=False, now=now))
    remaining = max(0, budget_lines - anchor_lines)
    # Walk from the end (excluding anchor), accumulating until budget.
    kept_tail_rev: list[Event] = []
    total = 0
    for i in range(len(events) - 1, 0, -1):
        rendered = render_event(events[i], in_flight=False, now=now)
        ev_lines = _count_lines(rendered)
        if kept_tail_rev and total + ev_lines > remaining:
            break
        kept_tail_rev.append(events[i])
        total += ev_lines
    kept_tail = list(reversed(kept_tail_rev))
    return [anchor, *kept_tail]


def card_page_info(state: CardState, user_id: int | None = None) -> tuple[int, int]:
    """Return (current_page_idx, total_pages) for the keyboard counter.

    Uses :func:`paginate_events_for_card` so the count reflects the
    budget-aware sub-pagination — matching what's actually rendered.
    ``user_id`` is only optional for legacy callers; passing it in
    yields the user-specific budget (otherwise default budget is used,
    which can mismatch the rendered card).
    """
    pages = paginate_events_for_card(state, user_id)
    total = max(1, len(pages))
    idx = _resolved_page_idx(state, total)
    return idx, total


def _is_stale(state: CardState) -> bool:
    if state.msg_id is None or state.last_event_ts <= 0:
        return False
    return (time.time() - state.last_event_ts) >= STALE_CARD_SECONDS


def _duplicate_of_seeded(events: list[Event], candidate: Event) -> bool:
    """True when ``candidate`` already appears in ``events``.

    Guards the live-append path against double-rendering a turn that a
    JSONL re-seed already pulled in. When a stale card is wiped and
    re-seeded (``_update_session_card_locked`` /
    ``release_card_message`` → ``_ensure_seeded``), the seed re-reads the
    transcript — which already contains the just-submitted user prompt
    that triggered this very update — and the same message is then
    appended again as the live event, rendering the user's message
    twice.

    Matched on ``(type, started_at, text)``. ``started_at`` is the JSONL
    timestamp parsed deterministically by ``_parse_timestamp``, so the
    seeded copy and the live copy of one entry share a bit-identical
    value while two distinct turns never collide (distinct timestamps).
    A user legitimately repeating the same text lands a new JSONL entry
    with a later timestamp, so it is not deduped.
    """
    for ev in events:
        if (
            ev.type == candidate.type
            and ev.started_at == candidate.started_at
            and ev.text == candidate.text
        ):
            return True
    return False


def _card_is_busy(state: CardState) -> bool:
    """Is this card actually producing output right now? Drives the
    Stop ↔ Kill keyboard split AND the polling-side TYPING indicator.

    Busy iff ALL of:
      1. ``msg_id`` set (card alive).
      2. There IS an event log AND its tail is not a terminal event
         (``final_text`` / ``error``). After ``finalize_task`` lands
         a ``final_text`` chunk the turn is done — TYPING and the
         Stop button should clear immediately, not linger for the
         grace window.
      3. Last event was within ``2 × CARD_EDIT_LAG`` (bridges the
         100-500 ms ``tool_use`` ↔ ``tool_result`` gap; longer gaps
         where claude is silently thinking are picked up by
         ``status_polling`` via the pane spinner instead).
    """
    from ..config import config

    if state.msg_id is None:
        return False
    if state.last_event_ts <= 0:
        return False
    if not state.events:
        return False
    last = state.events[-1]
    if last.type in ("final_text", "error"):
        return False
    now = time.time()
    grace = max(2.0, config.card_edit_lag * 2)
    return (now - state.last_event_ts) < grace


def _latest_inflight_idx(page_events: list[Event]) -> int | None:
    """Index of the last in-flight event on a page, or None if none."""
    for i in range(len(page_events) - 1, -1, -1):
        if _is_in_flight(page_events[i], page_events, i):
            return i
    return None
