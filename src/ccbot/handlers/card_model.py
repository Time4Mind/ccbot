"""Pure model + render layer for the live card.

This module holds the stateless, side-effect-free building blocks behind
``handlers.notifications``: the ``Event`` / ``CardState`` dataclasses, the
event builders (``_build_event`` / ``_apply_tool_result``), the
MarkdownV2-size estimators, and every ``render_*`` / ``paginate_*`` /
budget-trimming helper that turns a ``CardState`` into the text painted on
a Telegram message.

Nothing here touches the module-global card registries
(``_cards`` / ``_card_locks`` / ``_repost_intent`` / ``_msg_to_session``)
or sends / edits Telegram messages — those live in
``handlers.notifications`` (the lifecycle / facade module), which
re-exports every name defined here so existing
``from ccbot.handlers.notifications import X`` and ``notifications.X``
call sites keep resolving unchanged.

A "card" is a single Telegram message that the bot keeps editMessageText-
updating as Claude emits tool calls, thinking blocks, and text chunks.
Within a single card, content is paginated. Each ``Event`` with
``is_page_break=True`` (currently end_turn assistant text) becomes the
top of a new page; everything preceding it goes on the previous page.
Default focus = the page anchored to the latest answer.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field

from ..session import Session, session_manager
from ..session_monitor import NewMessage
from . import bg_status
from .switcher import session_emoji

# Listing the underscore-prefixed helpers in ``__all__`` marks them as
# the module's intended public interface so pyright's strict
# ``reportPrivateUsage`` doesn't flag the facade re-exports in
# ``handlers.notifications`` (which must keep these names importable as
# ``notifications.<name>`` for existing callers and the test suite).
__all__ = [
    "CARD_HARD_LIMIT",
    "CARD_MAX_EVENTS",
    "CARD_PAGE_BUDGET",
    "CARD_PAGE_LINES_DEFAULT",
    "CARD_PAGE_LINES_OVERSHOOT",
    "CARD_SEED_TURNS",
    "SPOILER_MAX_LINES",
    "STALE_CARD_SECONDS",
    "CardState",
    "Event",
    "_apply_tool_result",
    "_build_event",
    "_card_is_busy",
    "_chunk_final_text",
    "_count_lines",
    "_duplicate_of_seeded",
    "_estimate_md_v2_size",
    "_extract_expquote_inner",
    "_format_hhmmss",
    "_is_in_flight",
    "_is_stale",
    "_latest_inflight_idx",
    "_rechunk_oversized_finals_inplace",
    "_render_card",
    "_resolve_line_budget",
    "_resolved_page_idx",
    "_spoiler_body",
    "_split_page_by_budget",
    "_split_tool_text",
    "_strip_for_card",
    "_trim",
    "_trim_page_events",
    "card_page_info",
    "paginate_events",
    "paginate_events_for_card",
    "render_event",
    "render_page",
]

# Hard cap for rendered card text — Telegram limit is 4096; leave headroom.
CARD_HARD_LIMIT = 3800
# Number of accumulated events kept; older events still live in state.events
# but only the last N participate in pagination (FIFO eviction beyond this).
CARD_MAX_EVENTS = 5000
# After this much idleness, the next event opens a fresh card.
STALE_CARD_SECONDS = 5 * 60
# Max lines of body shown inside each tool/thinking spoiler. Overflow is
# truncated with a "… (+N more lines)" trailer. Env-tunable.
SPOILER_MAX_LINES = 5

# Char budget for one rendered card page — kept as a hard ceiling for
# the Telegram-level 4096-char limit. Headroom for header / divider /
# bg-panel. The user-facing budget is in LINES (see ``card_page_lines``
# user-setting / ``_resolve_line_budget``); chars budget here is only
# a sanity-cap when the page-by-lines result would still overflow TG.
CARD_PAGE_BUDGET = 3500

# Default page-size budget in LINES (logical \n-delimited rows in the
# MarkdownV2 source — close enough to visual lines on a phone for ±5
# tolerance the user explicitly accepted). User overrides via
# Settings → Page size (10 / 20 / 40 / 70).
CARD_PAGE_LINES_DEFAULT = 20

# Allowed overshoot (in lines) when trimming a page or chunking an
# anchor so a sentence / paragraph isn't broken mid-content.
CARD_PAGE_LINES_OVERSHOOT = 5

# Number of trailing end_turn boundaries to pull from JSONL when seeding
# an empty ``state.events`` (e.g. after a bot restart). Each end_turn
# becomes a page boundary, so this caps the "scrollback depth" of the
# card without re-reading the full transcript on every event.
CARD_SEED_TURNS = 20


@dataclass
class Event:
    """One unit of conversation rendered on the card.

    ``type`` discriminates render behaviour:

      - ``user_msg``   — user's typed text echoed via ``👤``
      - ``thinking``   — claude thinking block (``∴``)
      - ``tool_use``   — tool invocation (``▷``); on tool_result the
        same Event's ``completed_at`` flips and ``body`` becomes the
        result text.  ``tool_use_id`` matches assistant→user pairing.
      - ``text``       — mid-stream assistant text (stop_reason=tool_use)
      - ``final_text`` — end-of-turn assistant answer; ``is_page_break``
      - ``error``      — error-only event; ``is_page_break``
      - ``interactive``— AskUserQuestion / ExitPlanMode / Permission;
        rendered as a separate Telegram message, NOT in card body, but
        recorded here for page-break anchoring.
      - ``divider``    — historical "Результат" divider line; legacy
    """

    type: str
    text: str  # one-line header content (args summary / first line)
    started_at: float  # epoch seconds; HH:MM in header is derived from this
    body: str = ""  # full content under expandable blockquote
    completed_at: float | None = None  # set when event completes
    tool_use_id: str | None = None
    tool_name: str | None = None
    is_page_break: bool = False  # this event starts a new page
    is_error: bool = False
    image_data: list[tuple[str, bytes]] | None = None  # tool_result images


@dataclass
class CardState:
    msg_id: int | None = None
    events: list[Event] = field(default_factory=list)
    # Page the user is currently looking at. ``None`` = default focus
    # (page with the latest answer-anchor). Set by pagination callbacks.
    current_page_idx: int | None = None
    last_event_ts: float = 0.0
    last_rendered: str = ""  # last text we sent to TG; skips no-op edits
    last_edit_ts: float = 0.0  # monotonic seconds; gate for CARD_EDIT_LAG coalescing
    pending_edit: asyncio.Task[None] | None = None  # one deferred edit task at most
    is_continuation: bool = False  # True after a stale-pause or overflow split
    # User opened ≡ Menu / a sub-screen on the card's message. While set,
    # session updates accumulate into ``events`` but are NOT rendered to
    # Telegram — otherwise the next event would overwrite whatever menu
    # screen the user is looking at. Cleared by ``resume_card_view``
    # (called from text_handler when the user types) or implicitly
    # when the card is reset.
    in_menu_view: bool = False
    # kb-mode auto-persistence (Task #41). When claude shows an
    # interactive prompt (AskUserQuestion / ExitPlanMode / Permission),
    # the card msg is EDITED in place to show the prompt content + kb
    # navigation keyboard (3×3 grid). One msg per session — no separate
    # push. State machine:
    #   kb_prompt non-empty + in_kb_mode=True  → card msg = kb-mode view
    #   kb_prompt non-empty + in_kb_mode=False → user tapped Back; card
    #     shows regular view but with [🔙 Resume action] on Shot slot
    #   kb_prompt empty                        → no pending action
    kb_prompt: str = ""  # current prompt content (snapshot from pane)
    kb_ui_name: str = ""  # AskUserQuestion / ExitPlanMode / Permission
    in_kb_mode: bool = False
    # Inline-screenshots mode (Task #48). When the user has
    # ``card_inline_screenshots=True`` and the active session is this
    # one, the card msg is a photo+caption Telegram message (the photo
    # is a render of the tmux pane). On toggle off, the msg_id is reset
    # so the next event creates a fresh text-mode card.
    is_photo_msg: bool = False
    last_pane_hash: str = ""  # md5 of last captured pane text
    last_photo_edit_ts: float = 0.0  # monotonic seconds; 3s throttle
    # Cached context-window fill percentage for the active session, set by
    # session_events whenever a new assistant turn lands. Rendered as a
    # ``context: N%`` line above the bg-status panel. None = unknown.
    context_pct: int | None = None
    # JSONL-seed bookkeeping (A6). ``_ensure_seeded`` reads the recent
    # transcript exactly once per (re)set so the live card lands with
    # context after a restart. The wipe sites that empty ``events`` mid-
    # session for a NON-destructive reason (stale-pause reset, carrier
    # release on switcher tap) clear this flag so the next event re-seeds
    # — otherwise the card rebuilds one event at a time and the footer
    # page counter transiently collapses to ``1/1`` while the underlying
    # transcript still spans many turn-pages. ``/clear`` leaves it True:
    # that is an intentional wipe-to-zero.
    seed_attempted: bool = False


def _trim(s: str, limit: int = 200) -> str:
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        return s[: limit - 1] + "…"
    return s


_EXPQUOTE_BLOCK_RE = re.compile(
    r"\x02EXPQUOTE_START\x02.*?\x02EXPQUOTE_END\x02",
    re.DOTALL,
)
# Drop residual EXPQUOTE_START / EXPQUOTE_END sentinels that didn't
# pair up (transcript_format builds tool blocks with nested sentinels;
# the outer pair gets stripped but the inner one can leak in body).
_EXPQUOTE_ANY_RE = re.compile(r"\x02EXPQUOTE_(?:START|END)\x02")
# Pull the inner content out of an EXPQUOTE_START / END pair.
_EXPQUOTE_INNER_RE = re.compile(
    r"\x02EXPQUOTE_START\x02(.*?)\x02EXPQUOTE_END\x02",
    re.DOTALL,
)


def _extract_expquote_inner(text: str) -> str:
    """Return the content between the FIRST EXPQUOTE_START / END pair."""
    m = _EXPQUOTE_INNER_RE.search(text or "")
    return m.group(1) if m else ""


def _strip_for_card(text: str) -> str:
    """Strip residue that would render literally in MarkdownV2 mode.

    Card text is now sent with ``parse_mode=MarkdownV2`` (via
    ``send_with_fallback`` / ``_send_card_md``), so MarkdownV2 markers
    like ``**bold**`` get rendered properly. We only strip:

    * The full ``EXPQUOTE_START … EXPQUOTE_END`` block when it appears
      INSIDE a head line (heads are one-liners; the embedded quote
      belongs in the body, not the head).
    * Any orphan ``EXPQUOTE_*`` sentinel that escaped pair-matching.
    * ``$HOME`` → ``~`` so long Mac paths don't waste 30+ chars.

    The MarkdownV2 ``convert_markdown`` step inside ``send_with_fallback``
    handles escaping special chars and expanding paired EXPQUOTE blocks
    into expandable blockquote syntax.
    """
    import os

    out = _EXPQUOTE_BLOCK_RE.sub("", text)
    out = _EXPQUOTE_ANY_RE.sub("", out)
    home = os.path.expanduser("~")
    if home and home != "/":
        out = out.replace(home, "~")
    return out


def _parse_timestamp(ts: str) -> float:
    """Parse ISO-8601 timestamp from a JSONL entry into epoch seconds.

    Returns ``time.time()`` when the input is empty or unparseable so
    callers can use the result unconditionally as an ``started_at``.
    """
    if not ts:
        return time.time()
    try:
        import datetime as _dt

        # Tolerate trailing Z + offset forms; fromisoformat handles "+HH:MM"
        # natively but historically chokes on "Z".
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return time.time()


_TOOL_HEAD_RE = re.compile(
    r"^\s*\**(?P<name>[A-Za-z][\w-]*)\**\s*\((?P<args>.*)\)\s*$", re.DOTALL
)


def _split_tool_text(raw: str) -> tuple[str, str, str, str]:
    """Split transcript_format's tool text into name / args / summary / content.

    ``raw`` reaches us in the shape::

        **ToolName**(args)            ← head_block (can span multiple
                                        lines if args = bash heredoc /
                                        multi-line edit diff)
          ⎿  Output N lines           ← summary line (optional)
        \\x02EXPQUOTE_START\\x02<content>\\x02EXPQUOTE_END\\x02

    The summary line starts with whitespace + ``⎿``. Everything before
    that marker (or before the EXPQUOTE_START sentinel, whichever comes
    first) is the head_block — possibly multiple lines when args
    contains literal newlines (bash heredoc).

    Returns ``(name, args, summary, content)`` where:

    * ``name``    = bare tool name (``Bash``, ``Read``, ``Edit``).
    * ``args``    = whatever was between the outermost parens — pushed
      under the spoiler so long commands don't blow up the head line.
    * ``summary`` = ``⎿`` line content (``Output 5 lines``).
    * ``content`` = inside the EXPQUOTE block, minus duplicate head /
      summary lines that transcript_parser sometimes re-embeds.

    When the head doesn't parse as ``Name(args)`` (orphan tool_result
    fallback or weird format), the full head_block lands in ``name``
    and ``args`` is empty.
    """
    if not raw:
        return "", "", "", ""

    # Locate end-of-head: the first ``\n  ⎿`` summary marker OR the
    # first ``\x02EXPQUOTE_START\x02`` content marker, whichever comes
    # earlier. Whatever's BEFORE that boundary is the head_block (may
    # span multiple lines when args is a bash heredoc).
    summary_marker_re = re.compile(r"\n\s*⎿")
    summary_match = summary_marker_re.search(raw)
    quote_idx = raw.find("\x02EXPQUOTE_START\x02")
    head_end = len(raw)
    if summary_match is not None:
        head_end = min(head_end, summary_match.start())
    if quote_idx >= 0:
        head_end = min(head_end, quote_idx)
    head_block = raw[:head_end].rstrip("\n")

    name = _strip_for_card(head_block)
    args = ""
    m = _TOOL_HEAD_RE.match(head_block)
    if m:
        name = m.group("name").strip()
        args = m.group("args").strip()
        # The first-line ``Name(`` prefix being matched means the regex
        # already used DOTALL — args may legitimately contain newlines.

    summary = ""
    after_head = raw[head_end:]
    if after_head.startswith("\n"):
        after_head = after_head[1:]
    # Pull the summary line if it's first.
    if after_head.lstrip(" ").startswith("⎿"):
        nl = after_head.find("\n")
        if nl == -1:
            summary_line = after_head
            after_head = ""
        else:
            summary_line = after_head[:nl]
            after_head = after_head[nl + 1 :]
        summary = _strip_for_card(summary_line.lstrip(" ").lstrip("⎿").strip())

    # ``after_head`` is now either an EXPQUOTE block or plain rest.
    inner = _extract_expquote_inner(after_head) if after_head else ""
    content = inner if inner else after_head
    # Drop duplicate head/summary rows that transcript_parser may
    # re-embed at the top of the EXPQUOTE block.
    if content:
        content_lines = content.split("\n")
        first_norm = _strip_for_card(content_lines[0]).strip()
        head_norm = _strip_for_card(head_block).strip()
        if (
            first_norm == head_norm
            or (head_norm and first_norm.endswith(head_norm))
            or (
                first_norm.startswith(("✓ ", "▷ ", "✗ "))
                and head_norm
                and head_norm in first_norm
            )
        ):
            content_lines = content_lines[1:]
        if content_lines and content_lines[0].lstrip().startswith("⎿"):
            content_lines = content_lines[1:]
        content = "\n".join(content_lines).strip("\n")
    return name, args, summary, content


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
        # args (the command / file path) go under the spoiler so they
        # don't dominate the head line. body = args + content, args
        # first so the user sees the command on first spoiler line.
        spoiler_body = args
        if content:
            spoiler_body = f"{spoiler_body}\n{content}" if args else content
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
        # 12 lines"). args goes under spoiler with content.
        head_with_summary = (
            f"{name} · {summary}" if (name and summary) else name or summary
        )
        spoiler_body = args
        if content:
            spoiler_body = f"{spoiler_body}\n{content}" if args else content
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


# ─── Render helpers ───────────────────────────────────────────────────


def _format_elapsed(seconds: float) -> str:
    """Format ``M:SS`` for an elapsed timer (negative → ``0:00``)."""
    s = max(0, int(seconds))
    return f"{s // 60}:{s % 60:02d}"


def _format_hhmm(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M")


def _format_hhmmss(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


def _is_in_flight(event: Event, events: list[Event], idx: int) -> bool:
    """Per spec: ``⏳`` lives only on the LAST event of the latest page.

    Older events with ``completed_at=None`` are implicitly considered
    finished by virtue of a newer event having started — for the user
    the timer "moves" with whatever block claude is currently producing.
    """
    if idx != len(events) - 1:
        return False
    if event.completed_at is not None:
        return False
    return event.type in ("tool_use", "thinking", "text")


def _body_trim(body: str, max_lines: int = SPOILER_MAX_LINES) -> str:
    """Trim body content to ``max_lines`` lines. Excess → ``… (+N more lines)``."""
    if not body:
        return ""
    lines = body.split("\n")
    if len(lines) <= max_lines:
        return body
    kept = lines[:max_lines]
    extra = len(lines) - max_lines
    kept.append(f"… (+{extra} more lines)")
    return "\n".join(kept)


_SENTENCE_END_RE = re.compile(r"[.!?][\s)\]\"»]*\s")


def _table_continuation_prefix(chunk: str, remaining: str) -> str:
    """Header+separator rows to prepend when a cut landed inside a GFM table.

    If ``chunk`` ends with table rows (a trailing run of ``|``-lines whose
    first two are a header + ``|---|`` separator) and ``remaining`` starts
    with another table row, the table was split mid-body — the continuation
    page would render as headerless junk. Returns ``"header\\nsep\\n"`` so
    the caller can re-emit them; ``""`` when no table was cut.
    """
    rem_first = remaining.lstrip("\n").split("\n", 1)[0]
    if not rem_first.lstrip().startswith("|"):
        return ""
    lines = chunk.rstrip("\n").split("\n")
    i = len(lines)
    while i > 0 and lines[i - 1].lstrip().startswith("|"):
        i -= 1
    run = lines[i:]
    if len(run) < 2:
        return ""
    sep_core = run[1].strip().strip("|").replace("|", "").replace(" ", "")
    if not sep_core or not set(sep_core) <= {"-", ":"}:
        return ""
    return run[0] + "\n" + run[1] + "\n"


def _chunk_final_text(
    text: str,
    budget_lines: int = CARD_PAGE_LINES_DEFAULT,
    byte_budget: int = CARD_PAGE_BUDGET,
) -> list[str]:
    """Split a long final answer into chunks ≤ ``budget_lines`` AND ≤ ``byte_budget``.

    Smart-boundary preference (per user spec): paragraph (``\\n\\n``) →
    line (``\\n``) → sentence terminator (``.!?``) → word (space) → hard.
    Allows up to ``CARD_PAGE_LINES_OVERSHOOT`` extra lines so a sentence
    isn't broken mid-content. NEVER breaks mid-word.

    The byte cap mirrors Telegram's 4096-byte edit limit (with headroom
    for header / divider / footer / bg-panel). Without it, a wide
    single-paragraph answer can pass the line cap and still overflow
    after MarkdownV2 escaping — every reserved char gets a ``\\``
    prefix, blowing the rendered size past the limit.

    Empty / short input returns a single-chunk list.
    """
    if not text:
        return []
    if _count_lines(text) <= budget_lines and _estimate_md_v2_size(text) <= byte_budget:
        return [text]

    chunks: list[str] = []
    remaining = text
    while (
        _count_lines(remaining) > budget_lines
        or _estimate_md_v2_size(remaining) > byte_budget
    ):
        rem_lines = remaining.split("\n")
        # 1. Paragraph break — look at the last \n\n within budget+overshoot,
        #    clamped to the byte budget so we never search past the safe
        #    rendered-size window.
        cap = budget_lines + CARD_PAGE_LINES_OVERSHOOT
        char_cap_lines = sum(
            len(rem_lines[i]) + 1 for i in range(min(cap, len(rem_lines)))
        )
        char_cap_bytes = _char_pos_at_byte_budget(remaining, byte_budget)
        char_cap = (
            min(char_cap_lines, char_cap_bytes) if char_cap_bytes else char_cap_lines
        )
        # If even one char is over byte budget, char_cap_bytes is 0 — use a
        # minimal cap so the boundary scans still see SOMETHING. Edge case.
        if char_cap <= 0:
            char_cap = max(1, char_cap_lines)
        cut = remaining.rfind("\n\n", 0, char_cap)

        # 2. Line break within budget (no overshoot).
        if cut <= 0:
            char_budget = sum(
                len(rem_lines[i]) + 1 for i in range(min(budget_lines, len(rem_lines)))
            )
            char_budget = min(char_budget, char_cap)
            cut = remaining.rfind("\n", 0, char_budget)

        # 3. Sentence terminator within budget+overshoot.
        if cut <= 0:
            m_iter = list(_SENTENCE_END_RE.finditer(remaining[:char_cap]))
            if m_iter:
                cut = m_iter[-1].end()

        # 4. Word boundary within budget+overshoot.
        if cut <= 0:
            cut = remaining.rfind(" ", 0, char_cap)

        # 5. Hard cut (last resort — only if no other boundary found in
        #    the entire overshoot window). Use char_cap to avoid mid-word
        #    if possible; otherwise raw budget cut.
        if cut <= 0:
            cut = char_cap if char_cap > 0 else len(remaining)

        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n").lstrip(" ")
        # Cut landed inside a GFM table → re-emit header+separator so the
        # next page renders as a valid table. Length guard keeps forward
        # progress (never prepend more than the cut consumed).
        if chunk:
            prefix = _table_continuation_prefix(chunk, remaining)
            if prefix and len(prefix) < cut:
                remaining = prefix + remaining
    if remaining:
        chunks.append(remaining)
    return chunks


def _spoiler_body(body: str) -> str:
    """Wrap ``body`` in EXPQUOTE_START/END so MarkdownV2 conversion turns
    it into an expandable blockquote.

    Trims to ``SPOILER_MAX_LINES`` first, drops the home-path noise,
    keeps MarkdownV2 markers (``**bold**`` etc.) intact — they get
    properly rendered by ``convert_markdown`` at send time.
    """
    from ..transcript_format import format_expandable_quote

    trimmed = _body_trim(_strip_for_card(body))
    if not trimmed:
        return ""
    return format_expandable_quote(trimmed)


def render_event(event: Event, *, in_flight: bool, now: float) -> str:
    """Render one Event as a plain-text block for the card."""
    # Build the trailing time-or-elapsed marker
    if in_flight:
        marker = f" · ⏳ {_format_elapsed(now - event.started_at)}"
    elif event.type in ("tool_use", "thinking", "text"):
        marker = f" · {_format_hhmm(event.started_at)}"
    else:
        marker = ""

    if event.type == "user_msg":
        return f"👤 {event.text}"

    if event.type == "thinking":
        head = f"∴ thinking{marker}"
        body = _spoiler_body(event.body)
        return f"{head}\n{body}" if body else head

    if event.type == "tool_use":
        if event.is_error:
            glyph = "✗"
        elif in_flight:
            glyph = "▷"
        else:
            glyph = "✓"
        head = f"{glyph} {event.text}{marker}"
        body = _spoiler_body(event.body)
        return f"{head}\n{body}" if body else head

    if event.type == "tool_result":
        # Fallback when the matching tool_use Event isn't found (parser
        # race / restart). Render as a standalone row.
        head = f"✓ {event.text}{marker}"
        body = _spoiler_body(event.body)
        return f"{head}\n{body}" if body else head

    if event.type in ("text", "final_text", "error"):
        # Mid-stream / final / error — inline, no glyph.
        return event.text

    return event.text


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


# ─── Card composition ─────────────────────────────────────────────────


# Box-drawing / block-element glyphs (U+2500–U+259F). Claude Code's
# AskUserQuestion renders each option's ``preview`` inside a box-drawing
# frame (``┌ │ ├ ─ …``); captured verbatim into the kb-mode card those
# borders mangle the body. We strip them on the kb-mode path.
_BOX_DRAWING_RE = re.compile(r"[─-▟]")
_BORDER_ONLY_LINE_RE = re.compile(r"^[\s─-▟]*$")
# Box-drawing FRAME glyphs (verticals + corners + junctions + double-line),
# EXCLUDING the plain horizontals ─ ━ which show up as benign dividers in
# otherwise-normal prompts. Their presence is the signal that Claude Code
# framed the option previews in boxes (the case that mangles the card). A
# normal prompt — even one carrying a ── divider — matches none of these, so
# the sanitize/code-fence path stays a strict no-op for the well-behaved case.
_BOX_FRAME_RE = re.compile(r"[│┃┌-╋═-╬]")


def _sanitize_prompt_block(text: str) -> str:
    """Strip terminal box-drawing borders from a captured interactive prompt.

    Drops border-only lines and removes box-drawing glyphs from content
    lines (preserving indentation + internal spacing). Collapses 3+ blank
    lines that the border removal can leave behind.
    """
    out: list[str] = []
    for line in text.splitlines():
        if _BORDER_ONLY_LINE_RE.match(line):
            # Keep a single blank as a paragraph break, drop runs.
            if out and out[-1] != "":
                out.append("")
            continue
        cleaned = _BOX_DRAWING_RE.sub("", line).rstrip()
        out.append(cleaned)
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _render_card(
    sess: Session,
    state: CardState,
    *,
    footer: str = "",
    user_id: int | None = None,
) -> str:
    emoji = session_emoji(sess)
    state_label = sess.state
    cont_marker = " · …continued" if state.is_continuation else ""
    # Last-event timestamp in the header — HH:MM:SS of the most recent
    # event of any kind (per-event timestamps inside the body stay HH:MM).
    ts_suffix = ""
    if state.last_event_ts > 0:
        ts_suffix = " · " + _format_hhmmss(state.last_event_ts)
    name_part = sess.name or sess.id
    header = f"{emoji} *{name_part}* · {state_label}{cont_marker}{ts_suffix}"
    if sess.goal:
        header += f"\ngoal: {sess.goal}"

    # kb-mode view: card msg shows the interactive prompt content + kb
    # keyboard. The regular event log is BELOW the keyboard (footer'd by
    # the keyboard rather than by switcher/pagination). See Task #41.
    if state.in_kb_mode and state.kb_prompt:
        raw = state.kb_prompt
        if _BOX_FRAME_RE.search(raw):
            # Claude Code framed the option previews in box-drawing boxes
            # (┌ │ ├ …). Captured verbatim those borders mangle the body and
            # telegramify collapses the long region into an expandable
            # blockquote (the "✂ N lines hidden" artifact). Strip the borders
            # and render as a fenced code block — literal monospace, no
            # MarkdownV2 escaping, no blockquote collapse. Guard a stray ```.
            body = _sanitize_prompt_block(raw)
            prompt_part = body if "```" in body else f"```\n{body}\n```"
        else:
            # No box frame → the long-standing well-behaved prompt. Render it
            # exactly as before so this fix never alters a normal prompt.
            prompt_part = raw
        parts = [header, "─────", "⌨ *Waiting for your input:*", prompt_part]
        return "\n".join(parts)

    # Budget is in LINES (per user setting ``card_page_lines``).
    line_budget = _resolve_line_budget(user_id)
    # Lazy re-chunk: if any final_text Event in state.events exceeds
    # the CURRENT budget (e.g. user just lowered Settings → Page size,
    # or budget changed since finalize_task), split it into multiple
    # final_text Events on the fly. This is what makes the budget
    # ULTIMATIVE per spec — even already-finalised answers get rebuilt
    # to fit the new size. Idempotent: chunks below budget stay intact.
    _rechunk_oversized_finals_inplace(state, line_budget)

    pages = paginate_events_for_card(state, user_id)
    idx = _resolved_page_idx(state, len(pages))

    # Optional bg-panel always lives at the bottom.
    panel = ""
    if user_id is not None:
        panel = bg_status.render_panel(user_id, active_session_id=sess.id)

    # Safety net: a sub-page should fit by construction, but a single
    # huge event (one tool_result well over budget) can still overflow
    # — and we can't split it (EXPQUOTE atomicity). When that happens
    # _trim_page_events keeps anchor + tail; the dropped events become
    # genuinely inaccessible (no prior sub-page covers them), so the
    # marker phrasing acknowledges that.
    page_events = _trim_page_events(pages[idx], line_budget)
    body = render_page(page_events, now=time.time())
    if len(page_events) < len(pages[idx]):
        dropped = len(pages[idx]) - len(page_events)
        body = f"… (+{dropped} events trimmed to fit)\n{body}"

    parts = [header, "─────"]
    if body:
        parts.append(body)
    if footer:
        parts.append("─────")
        parts.append(footer)
    # Active session's own context-fill — single line at the very
    # bottom of the card body, just above the bg-status panel.
    # See ``set_card_context_pct``.
    if state.context_pct is not None:
        parts.append(f"context: {state.context_pct}%")
    if panel:
        # The panel carries its own ``─── фон ───`` label-separator
        # (pivot #39 feedback: previously the bg-row glued to the last
        # body line).
        parts.append(panel)
    # Paragraph-break join (``\n\n``) — single ``\n`` is a CommonMark
    # soft break that the rich parser collapses to a space, glueing
    # ``header ───── body ───── footer`` onto one row instead of each
    # on its own line. Same trap we hit in /archive and the bg-panel.
    return "\n\n".join(parts)


def _count_lines(text: str) -> int:
    """Count logical \\n-delimited lines in a rendered string."""
    if not text:
        return 0
    return text.count("\n") + 1


# Telegram MarkdownV2 reserved chars — each one gains a leading ``\``
# during ``convert_markdown``. We use this as an upper-bound estimate of
# the post-render byte count without paying for a real telegramify
# round-trip on every event. The bound is sloppy on purpose: better to
# oversplit a long answer than to send a 4096+ byte payload and lose the
# whole card edit to ``Message_too_long``.
_MD_V2_ESCAPE_CHARS = frozenset("_*[]()~`>#+-=|{}.!\\")


def _estimate_md_v2_size(text: str) -> int:
    """Upper bound on ``len(convert_markdown(text))`` (chars / bytes-ASCII).

    Each MarkdownV2 reserved char contributes ``+1`` over the raw length
    for its escape backslash. Real telegramify-markdown sometimes leaves
    a few of these unescaped inside valid markdown tokens (``**bold**``
    etc.), but using an over-estimate is the safe direction — we'd
    rather chunk earlier than discover overflow at edit time.
    """
    if not text:
        return 0
    extra = sum(1 for c in text if c in _MD_V2_ESCAPE_CHARS)
    return len(text) + extra


def _char_pos_at_byte_budget(text: str, byte_budget: int) -> int:
    """Largest ``p`` such that ``_estimate_md_v2_size(text[:p]) <= byte_budget``.

    Returns ``len(text)`` if the whole string fits. Used by
    ``_chunk_final_text`` to clamp the boundary-search window when a
    long answer would otherwise overflow Telegram's 4096-byte edit cap
    even at very few visual lines.
    """
    if byte_budget <= 0 or not text:
        return 0
    size = 0
    for i, c in enumerate(text):
        bump = 2 if c in _MD_V2_ESCAPE_CHARS else 1
        if size + bump > byte_budget:
            return i
        size += bump
    return len(text)


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
