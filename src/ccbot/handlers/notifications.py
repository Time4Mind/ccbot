"""Live-card notifications for the active session.

A "card" is a single Telegram message that the bot keeps editMessageText-
updating as Claude emits tool calls, thinking blocks, and text chunks.
Only the **active** session paints its card to chat; background sessions
go through ``handlers.bg_status`` and surface as a compact panel at the
bottom of the active card.

A fresh card opens (a new TG message is sent) on:

  - long pause: previous card sat idle for >= STALE_CARD_SECONDS
  - ``repost_card`` (Settings → card_position = repost)
  - first event of a new session

Within a single card, content is paginated. Each ``Event`` with
``is_page_break=True`` (currently end_turn assistant text) becomes
the top of a new page; everything preceding it goes on the previous
page. Default focus = the page anchored to the latest answer.

The header line carries:

  ``<emoji> *<name>* [<quota>] · <state> · HH:MM``

— where HH:MM is the time of the last claude event so the user can
tell at a glance whether the card is fresh or has been quiet.

When a tool_result arrives matching a previous tool_use, the existing
tool_use Event is mutated in place (``completed_at`` set, body replaced
with the result), so the ``▷`` line flips to ``✓`` (or ``✗`` on error)
and the elapsed timer is replaced with the start-time HH:MM.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from telegram import Bot, InlineKeyboardMarkup
from telegram.error import BadRequest, RetryAfter

from ..config import config
from ..session import Session, session_manager
from ..session_monitor import NewMessage
from . import bg_status
from .message_sender import safe_send
from .menu import build_footer_keyboard
from .switcher import session_emoji
from .tg_format import Attachment, split_overflow

logger = logging.getLogger(__name__)


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
# Settings → Page size (15 / 30 / 50 / 100).
CARD_PAGE_LINES_DEFAULT = 30

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

    @property
    def is_tool(self) -> bool:
        return self.type in ("tool_use", "tool_result")


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


# Per-(user, session.id) card state.
_cards: dict[tuple[int, str], CardState] = {}

# Reverse lookup so reply-quote can route a one-shot user message to the
# session that owns the message being replied to. Capped via FIFO eviction.
_MSG_REGISTRY_LIMIT = 2000
_msg_to_session: dict[tuple[int, int], str] = {}


def _register_msg(user_id: int, message_id: int, session_id: str) -> None:
    """Remember which session a bot message belongs to for reply-quote routing."""
    key = (user_id, message_id)
    # Best-effort eviction: drop ~10% of the oldest entries when the cap
    # is hit. dict preserves insertion order in CPython 3.7+.
    if len(_msg_to_session) >= _MSG_REGISTRY_LIMIT and key not in _msg_to_session:
        drop = max(1, _MSG_REGISTRY_LIMIT // 10)
        for k in list(_msg_to_session.keys())[:drop]:
            _msg_to_session.pop(k, None)
    _msg_to_session[key] = session_id


def lookup_session_for_message(user_id: int, message_id: int) -> str | None:
    """Resolve a Telegram message id back to the Session.id it represents."""
    return _msg_to_session.get((user_id, message_id))


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


def _chunk_final_text(
    text: str, budget_lines: int = CARD_PAGE_LINES_DEFAULT
) -> list[str]:
    """Split a long final answer into chunks each ≤ ``budget_lines`` lines.

    Smart-boundary preference (per user spec): paragraph (``\\n\\n``) →
    line (``\\n``) → sentence terminator (``.!?``) → word (space) → hard.
    Allows up to ``CARD_PAGE_LINES_OVERSHOOT`` extra lines so a sentence
    isn't broken mid-content. NEVER breaks mid-word.

    Empty / short input returns a single-chunk list.
    """
    if not text:
        return []
    lines = text.split("\n")
    if len(lines) <= budget_lines:
        return [text]

    chunks: list[str] = []
    remaining = text
    while _count_lines(remaining) > budget_lines:
        rem_lines = remaining.split("\n")
        # 1. Paragraph break — look at the last \n\n within budget+overshoot.
        cap = budget_lines + CARD_PAGE_LINES_OVERSHOOT
        # Convert line cap to char index.
        char_cap = sum(len(rem_lines[i]) + 1 for i in range(min(cap, len(rem_lines))))
        cut = remaining.rfind("\n\n", 0, char_cap)

        # 2. Line break within budget (no overshoot).
        if cut <= 0:
            char_budget = sum(
                len(rem_lines[i]) + 1
                for i in range(min(budget_lines, len(rem_lines)))
            )
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
    """Split ``events`` into pages.

    Page break: each Event with ``is_page_break=True`` becomes the TOP
    of a new page (everything before it lives on the previous page).
    Empty input → ``[[]]`` so callers can address page 0.
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


def _resolved_page_idx(state: CardState, total_pages: int) -> int:
    """``current_page_idx`` clamped, with ``None`` → last (default focus)."""
    if total_pages <= 0:
        return 0
    if state.current_page_idx is None:
        return total_pages - 1
    return max(0, min(state.current_page_idx, total_pages - 1))


def render_page(events: list[Event], now: float) -> str:
    """Render the events of one page into a single body string.

    Events are separated by ``\\n\\n\\n`` (two blank lines in the
    rendered MarkdownV2 source). Telegram swallows one blank row after
    a closing expandable blockquote ``||``, so a single ``\\n\\n``
    collapses to zero visible gap and the page reads as a wall of text.
    Two empty lines survive as exactly one visible blank row between
    tool/thinking blocks — the gap the user actually needs.
    """
    parts: list[str] = []
    for i, ev in enumerate(events):
        parts.append(render_event(ev, in_flight=_is_in_flight(ev, events, i), now=now))
    return "\n\n\n".join(parts)


# ─── Card composition ─────────────────────────────────────────────────


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

    pages = paginate_events(state.events)
    idx = _resolved_page_idx(state, len(pages))

    # Optional bg-panel always lives at the bottom.
    panel = ""
    if user_id is not None:
        panel = bg_status.render_panel(user_id, active_session_id=sess.id)

    # 4096-char enforcement (Telegram limit). Header + divider + footer
    # + panel take fixed prefix/suffix; body must fit in what's left
    # AFTER MarkdownV2 conversion (expansion can add 10-20% via escapes).
    # We trim by DROPPING OLDEST WHOLE EVENTS — never by cutting strings
    # mid-content, since the body uses EXPQUOTE_START/END sentinels that
    # MUST stay paired or the spoiler structure breaks ("text при
    # пагинации - ломается"). Older events stay reachable on previous
    # pages via ◀.
    # Budget is in LINES (per user setting ``card_page_lines``).
    # _trim_page_events drops middle events to fit; anchor + tail kept.
    line_budget = _resolve_line_budget(user_id)
    page_events = _trim_page_events(pages[idx], line_budget)
    body = render_page(page_events, now=time.time())
    if len(page_events) < len(pages[idx]):
        dropped = len(pages[idx]) - len(page_events)
        body = f"… (+{dropped} older events on previous pages)\n{body}"

    parts = [header, "─────"]
    if body:
        parts.append(body)
    if footer:
        parts.append("─────")
        parts.append(footer)
    if panel:
        parts.append(panel)
    return "\n".join(parts)


def _count_lines(text: str) -> int:
    """Count logical \\n-delimited lines in a rendered string."""
    if not text:
        return 0
    return text.count("\n") + 1


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
    if value not in (15, 30, 50, 100):
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


def card_page_info(state: CardState) -> tuple[int, int]:
    """Return (current_page_idx, total_pages) for the keyboard counter."""
    pages = paginate_events(state.events)
    total = max(1, len(pages))
    idx = _resolved_page_idx(state, total)
    return idx, total


def _is_stale(state: CardState) -> bool:
    if state.msg_id is None or state.last_event_ts <= 0:
        return False
    return (time.time() - state.last_event_ts) >= STALE_CARD_SECONDS


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
    try:
        claude_sess = await session_manager.resolve_session_for_window(sess.window_id)
    except Exception as e:
        logger.debug("seed: resolve_session_for_window failed: %s", e)
        return []
    if claude_sess is None or not claude_sess.file_path:
        return []
    file_path = claude_sess.file_path
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


async def _ensure_seeded(user_id: int, sess: Session, state: CardState) -> None:
    """Seed ``state.events`` from JSONL on first access after restart.

    No-op when events already exist or when seeding has been attempted
    before for this state. Idempotent — ``_seed_attempted`` flag on the
    CardState prevents repeated JSONL reads.
    """
    if state.events:
        return
    if getattr(state, "_seed_attempted", False):
        return
    state._seed_attempted = True  # type: ignore[attr-defined]
    # User-settable depth — Settings → Card history (10/20/50/100).
    try:
        max_turns = int(
            session_manager.get_user_settings(user_id).get(
                "card_history", CARD_SEED_TURNS
            )
        )
    except (TypeError, ValueError):
        max_turns = CARD_SEED_TURNS
    seeded = await _seed_events_from_jsonl(sess, max_turns=max_turns)
    if seeded:
        state.events = seeded
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


def _should_buffer(user_id: int, session_id: str, state: CardState) -> bool:
    """Return True when the live card must buffer events instead of
    rendering. Two reasons:

    1. The user has the carrier on a Menu / sub-screen
       (``state.in_menu_view`` — set by ``pause_card_view`` /
       ``transfer_card_to_carrier``, cleared by ``resume_card_view`` /
       ``release_card_message`` / ``detach_paused_cards_at_message``).
    2. The session is currently a background one for this user
       (``get_active_session(user_id).id != session_id``). Computed
       live, NOT stored — a session that's briefly bg and then active
       again recovers without help. (Earlier this was implemented as a
       sticky ``state.in_menu_view = True`` inside update_session_card;
       the flag never got cleared on becoming active again, so the card
       stayed paused forever — silent until the next typed message
       woke ``resume_card_view``. This helper makes the bg check live
       so that class of bug can't reoccur.)
    """
    if state.in_menu_view:
        return True
    active = session_manager.get_active_session(user_id)
    return active is None or active.id != session_id


def reset_card(user_id: int, session_id: str) -> None:
    """Drop the cached card so the next event creates a fresh message."""
    _cards.pop((user_id, session_id), None)


def pause_card_view(user_id: int, session_id: str) -> None:
    """Mark the live card paused so session updates buffer instead of
    rendering. Called when the user opens a Menu / sub-screen on the
    card's message — otherwise a stream of tool calls would overwrite
    whatever they're looking at."""
    state = _cards.get((user_id, session_id))
    if state is None:
        logger.info(
            "card_pause skip user=%d sess=%s reason=no_state",
            user_id,
            session_id,
            extra={
                "event": "card_pause_skip",
                "user_id": user_id,
                "session_id": session_id,
                "reason": "no_state",
            },
        )
        return
    state.in_menu_view = True
    logger.info(
        "card_pause user=%d sess=%s msg_id=%s lines=%d",
        user_id,
        session_id,
        state.msg_id,
        len(state.events),
        extra={
            "event": "card_pause",
            "user_id": user_id,
            "session_id": session_id,
            "msg_id": state.msg_id,
            "lines": len(state.events),
        },
    )


def transfer_card_to_carrier(
    user_id: int,
    from_session_id: str | None,
    to_session_id: str,
    target_message_id: int,
) -> None:
    """Hand off ownership of ``target_message_id`` from one session's
    live card to another's. Called when the switcher flips active.

    Effect:
      - FROM session is paused (``in_menu_view=True``) so its events
        buffer silently in ``state.events`` instead of editing the
        carrier (which now belongs to the TO session). No new chat
        message lands until the user switches back or types text.
      - TO session claims the carrier (``msg_id=target_message_id``)
        and its pause is released, so the next event for it renders
        on the carrier — overlaying the preview that the callback
        just painted.

    No-op when ``from_session_id == to_session_id`` (user tapped the
    already-active session). The previous live-card behaviour — where
    A's lingering ``msg_id`` clobbered B's preview every time A emitted
    a tool call — falls out naturally because A is now paused.
    """
    if from_session_id == to_session_id:
        logger.info(
            "card_transfer skip user=%d sess=%s reason=same_session",
            user_id,
            to_session_id,
            extra={
                "event": "card_transfer_skip",
                "user_id": user_id,
                "session_id": to_session_id,
                "reason": "same_session",
            },
        )
        return
    from_msg_id_was: int | None = None
    if from_session_id:
        from_state = _cards.get((user_id, from_session_id))
        if from_state is not None:
            from_msg_id_was = from_state.msg_id
            if (
                from_state.pending_edit is not None
                and not from_state.pending_edit.done()
            ):
                from_state.pending_edit.cancel()
            from_state.pending_edit = None
            from_state.in_menu_view = True
    to_state = _cards.setdefault((user_id, to_session_id), CardState())
    to_msg_id_was = to_state.msg_id
    if to_state.pending_edit is not None and not to_state.pending_edit.done():
        to_state.pending_edit.cancel()
    to_state.pending_edit = None
    to_state.msg_id = target_message_id
    # Pause the TO card across the switch window. The caller (CB_SW_USE)
    # will paint history on this message_id next, and then call
    # ``release_card_message`` which clears both ``msg_id`` and
    # ``in_menu_view``. If we left ``in_menu_view=False`` here, any bg
    # event arriving in the ~150 ms parse + edit window would trigger
    # ``refresh_panel`` — that path sees
    # ``msg_id=carrier`` + ``in_menu_view=False`` and rerenders the
    # live-card body over the carrier, clobbering the history paint
    # we're racing to land. Symptom: user sees "header + bg panel"
    # instead of transcript after a switch.
    to_state.in_menu_view = True
    logger.info(
        "card_transfer user=%d from=%s (from_msg=%s) to=%s (was_msg=%s) carrier=%s",
        user_id,
        from_session_id or "-",
        from_msg_id_was,
        to_session_id,
        to_msg_id_was,
        target_message_id,
        extra={
            "event": "card_transfer",
            "user_id": user_id,
            "from_session_id": from_session_id,
            "from_msg_id_was": from_msg_id_was,
            "to_session_id": to_session_id,
            "to_msg_id_was": to_msg_id_was,
            "carrier_msg_id": target_message_id,
        },
    )


def detach_paused_cards_at_message(user_id: int, message_id: int) -> None:
    """Release card state bound to ``message_id`` when the carrier has
    been repurposed for a different flow.

    The pause→resume design assumes the user eventually returns to the
    live card via ``resume_card_view`` (typing text, etc.). But when
    the carrier message gets hijacked for a different session — e.g.
    user navigates ``Menu → ＋ new`` and confirms a directory, the new
    session's "Created" status now owns the message — the OLD session's
    pause never gets released and its events buffer forever. Worse,
    ``state.msg_id`` still points at a message that's no longer its
    card, so a later edit would clobber whatever's there.

    This helper resets ``msg_id`` (so the next event opens a fresh
    card) and clears the pause flags for every card on this user that
    happened to be paused on the now-stolen message.
    """
    detached: list[str] = []
    for (uid, sid), state in list(_cards.items()):
        if uid != user_id or state.msg_id != message_id:
            continue
        if state.pending_edit is not None and not state.pending_edit.done():
            state.pending_edit.cancel()
        state.pending_edit = None
        state.msg_id = None
        state.in_menu_view = False
        # Mark continuation so the next card visually flags carry-over
        # (``…continued`` in the header).
        state.is_continuation = True
        detached.append(sid)
    if detached:
        logger.info(
            "card_detach user=%d msg=%s sessions=%s",
            user_id,
            message_id,
            detached,
            extra={
                "event": "card_detach",
                "user_id": user_id,
                "msg_id": message_id,
                "sessions": detached,
            },
        )


def release_card_message(user_id: int, session_id: str) -> None:
    """Drop the live-card binding to its current Telegram message_id
    without touching the message itself.

    Called from the switcher-tap handler right after history is painted
    on the carrier: the carrier now holds a frozen transcript view, and
    the TO session's live card must NOT keep editing it. With ``msg_id``
    cleared, the next claude event opens a fresh card below (carrying
    the bg-status panel and prior-context seed); the history carrier
    stays put and remains paginable.

    Buffered ``lines`` are also wiped — they were destined for the
    overwritten card; the fresh card starts empty on its next event.
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = None
    state.in_menu_view = False
    state.events = []
    state.last_rendered = ""
    state.is_continuation = True
    logger.info(
        "card_release user=%d sess=%s",
        user_id,
        session_id,
        extra={
            "event": "card_release",
            "user_id": user_id,
            "session_id": session_id,
        },
    )


async def resume_card_view(bot: Bot, user_id: int, sess: Session) -> None:
    """Drop the menu-pause so future events render again, and re-paint
    the carrier with the buffered events.

    CRITICAL: clears ``in_menu_view`` UNCONDITIONALLY when the state
    exists — even when ``msg_id`` was lost (carrier stale / deleted /
    not yet created). Earlier this returned early without clearing
    the pause, leaving the card stuck in ``must_buffer=True`` forever
    (symptom: chronic ``card_update buffered`` log, body never updates
    even though claude is producing events). When ``msg_id`` is None
    we still clear the flag; the next claude event spawns a fresh
    card via ``_send_card`` because ``state.msg_id is None``.
    """
    state = _cards.get((user_id, sess.id))
    if state is None:
        return
    state.in_menu_view = False
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    if state.msg_id is None:
        # Nothing to paint right now — the next event will create a
        # fresh card. The unpause above is the important part.
        return
    text = _render_card(sess, state, user_id=user_id)
    keyboard = build_footer_keyboard(user_id, screen="main", is_busy=True)
    if await _edit_card(bot, user_id, state, text=text, reply_markup=keyboard):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()


async def paint_card_on_carrier(
    bot: Bot,
    user_id: int,
    sess: Session,
    carrier_msg_id: int,
) -> None:
    """Claim ``carrier_msg_id`` as ``sess``'s live card and paint it.

    Used by Menu → Sessions: the carrier is the menu message the user just
    tapped, and we want it to become the live card (one unified surface
    instead of a separate list rendering). The previous ``state.msg_id``
    is left as a frozen artifact in chat — the next claude event uses
    the new carrier.
    """
    state = _cards.setdefault((user_id, sess.id), CardState())
    # Menu → Sessions on a fresh post-restart state: seed history first
    # so the user lands on a card with their conversation, not 1/1.
    await _ensure_seeded(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.msg_id = carrier_msg_id
    state.in_menu_view = False
    state.last_rendered = ""
    _register_msg(user_id, carrier_msg_id, sess.id)
    text = _render_card(sess, state, user_id=user_id)
    keyboard = build_footer_keyboard(
        user_id, screen="main", is_busy=_card_is_busy(state)
    )
    if await _edit_card(bot, user_id, state, text=text, reply_markup=keyboard):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
        # Migrate the switcher pointer onto the new carrier so previous
        # switcher rows in chat stop being the canonical surface.
        prev = session_manager.get_last_switcher_msg(user_id)
        if prev and prev != carrier_msg_id:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=prev, reply_markup=None
                )
            except Exception:
                pass
        session_manager.set_last_switcher_msg(user_id, carrier_msg_id)


async def clear_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Wipe the live card's body in response to a user-driven /clear.

    Edits the existing message to a header-only "(cleared)" snapshot
    so the user sees the previous tool log disappear, then drops the
    cached state so the next claude event spawns a fresh card.
    No-op when there is no live card.
    """
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        reset_card(user_id, sess.id)
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    state.events = []
    state.last_rendered = ""
    text = _render_card(sess, state, footer="(cleared)", user_id=user_id)
    cleared_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)
    await _edit_card(bot, user_id, state, text=text, reply_markup=cleared_kb)
    reset_card(user_id, sess.id)


def _card_is_busy(state: CardState) -> bool:
    """Is this card actually producing output right now? Drives the
    Stop ↔ Kill keyboard split AND the polling-side TYPING indicator.

      1. ``msg_id`` must be set (card alive — finalize hasn't reset).
      2. A recent claude event within ``2 × CARD_EDIT_LAG``. Bridges
         the 100-500 ms gap between ``tool_use`` and ``tool_result``
         where the event stream briefly clears.
    """
    if state.msg_id is None:
        return False
    if state.last_event_ts <= 0:
        return False
    now = time.time()
    grace = max(2.0, config.card_edit_lag * 2)
    return (now - state.last_event_ts) < grace


async def _send_card(
    bot: Bot,
    user_id: int,
    sess: Session,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a brand-new card message and remember it as the live card.

    ``reply_markup`` overrides the default footer keyboard. Used by
    ``finalize_task`` to attach the idle-state Kill row to a completed
    result instead of the busy-state Stop row.
    """
    if reply_markup is None:
        # Default: a fresh card is being sent because a turn is in
        # flight (update_session_card, repost_card, continuation
        # overflow). ``_card_is_busy`` keys off ``state.msg_id`` which
        # is still None at this point — the right signal here is
        # "we're sending a card", which by definition means Stop is
        # the user's intent. ``finalize_task`` overrides ``reply_markup``
        # explicitly with the Kill keyboard when a turn completes.
        reply_markup = build_footer_keyboard(user_id, screen="main", is_busy=True)
    keyboard = reply_markup
    # Send through MarkdownV2 with plain-text fallback so spoilers /
    # bold / italic render properly. ``send_with_fallback`` runs the
    # text through ``convert_markdown`` (expanding EXPQUOTE sentinels
    # into MarkdownV2 expandable blockquote syntax) and falls back to
    # ``strip_sentinels(text)`` on conversion error.
    from .message_sender import send_with_fallback

    try:
        sent = await send_with_fallback(
            bot,
            user_id,
            text,
            reply_markup=keyboard,
            disable_notification=True,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.debug("card send failed: %s", e)
        return
    if not sent:
        return
    # Strip the previous switcher (if any), then remember this one as the
    # carrier of the live switcher.
    prev = session_manager.get_last_switcher_msg(user_id)
    if prev and prev != sent.message_id:
        try:
            await bot.edit_message_reply_markup(
                chat_id=user_id, message_id=prev, reply_markup=None
            )
        except Exception:
            pass
    if keyboard is not None:
        session_manager.set_last_switcher_msg(user_id, sent.message_id)
    state.msg_id = sent.message_id
    state.last_rendered = text
    _register_msg(user_id, sent.message_id, sess.id)


async def _edit_card(
    bot: Bot,
    user_id: int,
    state: CardState,
    *,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> bool:
    """Edit the live card. Returns False if the edit failed permanently.

    Always sends a keyboard along with the text — relying on Telegram's
    "preserve keyboard when reply_markup is omitted" semantics turned out
    flaky (the buttons flickered between edits). Caller may pass an
    explicit `reply_markup`; otherwise we rebuild from current busy state.
    """
    if state.msg_id is None:
        return False
    # User is currently looking at a Menu / sub-screen on this card's
    # message. Don't repaint — would clobber whatever they're navigating.
    # State.lines keeps accumulating; resume_card_view will catch up.
    if state.in_menu_view:
        return True
    if reply_markup is None:
        reply_markup = build_footer_keyboard(
            user_id, screen="main", is_busy=_card_is_busy(state)
        )
    # Edit through MarkdownV2 with plain-text fallback so the live card
    # renders **bold**, EXPQUOTE expandable blockquotes, etc. properly.
    from ..markdown_v2 import convert_markdown
    from .message_sender import PARSE_MODE, strip_sentinels

    formatted = convert_markdown(text)
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=formatted,
            parse_mode=PARSE_MODE,
            reply_markup=reply_markup,
        )
        return True
    except BadRequest as e:
        err = str(e)
        if "Message is not modified" in err:
            return True
        if (
            "Message to edit not found" in err
            or "message can't be edited" in err.lower()
            or "MESSAGE_ID_INVALID" in err
        ):
            # Carrier is genuinely gone — reset msg_id so the next event
            # opens a fresh card.
            logger.info("card edit lost-carrier msg_id=%s err=%s", state.msg_id, err)
            state.msg_id = None
            return False
        # Parse error / can't render — fall back to stripped plain text
        # on the SAME carrier. Keep the card alive.
        logger.warning("card edit MarkdownV2 failed msg=%s err=%s", state.msg_id, err)
        try:
            await bot.edit_message_text(
                chat_id=user_id,
                message_id=state.msg_id,
                text=strip_sentinels(text),
                reply_markup=reply_markup,
            )
            return True
        except BadRequest as e2:
            err2 = str(e2)
            if "Message is not modified" in err2:
                return True
            logger.warning(
                "card edit plain fallback failed msg=%s err=%s", state.msg_id, err2
            )
        except RetryAfter:
            raise
        except Exception as e2:
            logger.warning(
                "card edit plain fallback exc msg=%s err=%s", state.msg_id, e2
            )
    except RetryAfter:
        raise
    except Exception as e:
        logger.warning("card edit failed (other): %s", e)
    return False


async def _deferred_edit(
    bot: Bot, user_id: int, sess: Session, state: CardState, delay: float
) -> None:
    """Sleep `delay` then render the latest card state and edit once.

    The deferred task always picks up the latest `state.events`, so multiple
    events arriving during the sleep collapse into a single edit.
    """
    try:
        await asyncio.sleep(delay)
        # Stale guard: card may have been reset (finalize_task) while we slept.
        if state.msg_id is None:
            return
        text = _render_card(sess, state, user_id=user_id)
        if text == state.last_rendered:
            return
        if await _edit_card(bot, user_id, state, text=text):
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.debug("deferred card edit failed: %s", e)
    finally:
        state.pending_edit = None


async def update_session_card(
    bot: Bot, user_id: int, sess: Session, msg: NewMessage
) -> None:
    """Append `msg` to the session's live card, or open a new one if needed.

    Triggers a fresh card on long pause and on hard-limit overflow.
    """
    # Fire a (throttled) background prewarm of the pages cache so the
    # live-card's ◀ Older N/N counter has a value to render on the
    # next event. The first event after session start may still paint
    # without the counter — the background task lands within a second.
    if sess.window_id:
        from .history import kick_prewarm

        kick_prewarm(sess.window_id)

    state = get_card_state(user_id, sess)
    # First event after a bot restart: pull JSONL history into events
    # so the card shows context, not a single 1/1 page.
    await _ensure_seeded(user_id, sess, state)

    # Should we buffer this event instead of rendering it now? Reasons:
    # - the user is on a Menu / sub-screen (state.in_menu_view set by
    #   pause_card_view or transfer_card_to_carrier);
    # - the session isn't the user's currently-active one (live check —
    #   bg sessions must stay silent in chat).
    # The check is in ``_should_buffer`` so future buffering reasons
    # converge on the same predicate. Previously the bg branch was
    # implemented by force-setting ``state.in_menu_view = True`` here;
    # the flag was sticky and outlived the bg phase, leaving the card
    # permanently paused — until a typed message woke
    # ``resume_card_view``. Computing the bg check live fixes that.
    must_buffer = _should_buffer(user_id, sess.id, state)

    msg_id_in = state.msg_id
    in_menu_view_in = state.in_menu_view

    new_event = _build_event(msg)
    # tool_result: fold into the matching tool_use Event in place.
    # If no match (race / restart), append the placeholder as a row.
    replaced = False
    if msg.content_type == "tool_result":
        replaced = _apply_tool_result(state, new_event)

    # Buffer-only path: user is on a menu/sub-screen OR session is bg.
    # Buffer the event into ``state.events`` so resume / next switcher
    # tap can catch up; do NOT trigger stale-card resets, overflow
    # continuations, or any rendering.
    if must_buffer:
        if not replaced:
            state.events.append(new_event)
        state.last_event_ts = time.time()
        logger.info(
            "card_update buffered sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            msg_id_in,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_buffered",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": msg_id_in,
                "content_type": msg.content_type,
                "lines": len(state.events),
                "in_menu_view": in_menu_view_in,
            },
        )
        return

    # Trigger: long pause → fresh card.
    if _is_stale(state):
        state.msg_id = None
        state.events = []
        state.current_page_idx = None
        state.is_continuation = True
        state.last_rendered = ""

    if not replaced:
        state.events.append(new_event)
        # User-action-anchor: when on the latest page, every new event
        # keeps the user there. Done as None (=stick-to-latest) so the
        # render layer picks the latest page automatically.
        # Page idx is recalibrated by paginate-aware callbacks.

    # Cap event log to avoid unbounded memory; FIFO evicts oldest.
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]

    state.last_event_ts = time.time()

    # Pagination handles size: the latest page is always within
    # CARD_HARD_LIMIT chars (paginate splits before the boundary).
    # No continuation-card path.

    text = _render_card(sess, state, user_id=user_id)

    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text)
        state.last_edit_ts = time.monotonic()
        logger.info(
            "card_update sent sess=%s msg_id=%s ctype=%s lines=%d",
            sess.id,
            state.msg_id,
            msg.content_type,
            len(state.events),
            extra={
                "event": "card_update_sent",
                "user_id": user_id,
                "session_id": sess.id,
                "msg_id": state.msg_id,
                "content_type": msg.content_type,
                "lines": len(state.events),
            },
        )
        return

    # Coalesce edits — at most one editMessageText per live_lag seconds.
    # User setting takes precedence over the env-var default.
    user_lag = session_manager.get_user_settings(user_id).get("live_lag")
    if user_lag is None:
        user_lag = config.card_edit_lag
    lag = max(0.0, float(user_lag))
    elapsed = time.monotonic() - state.last_edit_ts if state.last_edit_ts else lag
    if lag <= 0 or elapsed >= lag:
        edited = await _edit_card(bot, user_id, state, text=text)
        if edited:
            state.last_rendered = text
            state.last_edit_ts = time.monotonic()
            logger.info(
                "card_update edit sess=%s msg_id=%s ctype=%s lines=%d",
                sess.id,
                state.msg_id,
                msg.content_type,
                len(state.events),
                extra={
                    "event": "card_update_edited",
                    "user_id": user_id,
                    "session_id": sess.id,
                    "msg_id": state.msg_id,
                    "content_type": msg.content_type,
                    "lines": len(state.events),
                },
            )
        else:
            # Edit failed AND we couldn't recover — DO NOT fall back to
            # _send_card here. Sending a new message produces duplicate
            # cards in chat (this was the "2 messages in a row" bug:
            # Message_too_long → fallback send → stale card stays + new
            # appears). Caller's next event retries the edit; if the
            # carrier message is truly gone (deleted, too old), the
            # ``Message to edit not found`` branch in _edit_card resets
            # msg_id and a fresh card spawns on the next event.
            state.last_edit_ts = time.monotonic()
            logger.warning(
                "card_update edit_failed sess=%s msg_id=%s — keeping "
                "stale card; new render will retry on next event",
                sess.id,
                state.msg_id,
            )
        return

    # Inside the coalescing window: ensure exactly one deferred edit is queued.
    if state.pending_edit is None or state.pending_edit.done():
        delay = max(0.05, lag - elapsed)
        state.pending_edit = asyncio.create_task(
            _deferred_edit(bot, user_id, sess, state, delay)
        )


async def finalize_task(bot: Bot, user_id: int, sess: Session, final_text: str) -> None:
    """Append the final assistant answer to the current live card.

    Appends a ``final_text`` Event with ``is_page_break=True`` so the
    new answer anchors the top of the latest page; everything before
    it (tool log, thinking, mid-stream text) lives on the previous page.
    The user lands on the new latest page by default. Long answers
    that exceed Telegram's 4096-char limit are sub-paginated by
    ``paginate_events``.
    """
    state = get_card_state(user_id, sess)
    # First event after a bot restart: seed JSONL history before
    # appending the final answer so the user sees their context.
    await _ensure_seeded(user_id, sess, state)

    # Bg-session silence + menu-pause buffering. Same predicate as
    # update_session_card — see ``_should_buffer`` for the rationale.
    must_buffer = _should_buffer(user_id, sess.id, state)

    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None

    cleaned = (final_text or "").strip()
    if not cleaned:
        # No final text from Claude (e.g. /clear with nothing else).
        # Drop the card; no push — the previous "completion push"
        # behaviour was removed when the result moved into the card body.
        if must_buffer:
            return
        reset_card(user_id, sess.id)
        return

    formatted = split_overflow(cleaned)
    cleaned = formatted.text
    attachments = formatted.attachments

    # Final answer = ONE OR MORE is_page_break Events: each chunk
    # anchors a new page. ``_chunk_final_text`` keeps every chunk under
    # ``card_page_lines`` user-setting (in LINES) so the rendered page
    # respects what the user picked. Smart boundaries (paragraph / line
    # / sentence / word) prevent mid-content breaks. Default focus lands
    # on the FIRST chunk's page so the user reads the answer from the top.
    now = time.time()
    stripped_full = _strip_for_card(cleaned)
    chunks = _chunk_final_text(stripped_full, _resolve_line_budget(user_id))
    final_events = [
        Event(
            type="final_text",
            text=chunk,
            body=chunk,
            started_at=now,
            completed_at=now,
            is_page_break=True,
        )
        for chunk in chunks
    ]

    # Buffer-only path: user is on a Menu view OR session is bg.
    # Accumulate the answer Events into state. resume_card_view (next
    # typed message) / switcher tap will render the catch-up.
    # Attachments still go out (file delivery shouldn't wait on UI nav).
    if must_buffer:
        state.events.extend(final_events)
        state.last_event_ts = now
        if attachments:
            await _send_attachments(bot, user_id, attachments)
        return

    state.events.extend(final_events)
    if len(state.events) > CARD_MAX_EVENTS:
        del state.events[: len(state.events) - CARD_MAX_EVENTS]
    state.last_event_ts = now
    # Default focus: when the answer was split into N chunks, land on
    # the FIRST chunk's page so the user starts at the top. When there's
    # only one chunk, ``None`` = latest, which is that same page.
    if len(final_events) > 1:
        pages_after = paginate_events(state.events)
        # The first chunk's page is at index (len(pages) - len(chunks)).
        first_chunk_page = max(0, len(pages_after) - len(final_events))
        state.current_page_idx = first_chunk_page
    else:
        state.current_page_idx = None

    # Refresh the pages cache so the live-card's pagination counter
    # reflects the final transcript length on the finalised message.
    # This is the one place we await prewarm directly — finalize fires
    # once per task, so ~1 s of parsing is OK to pay for a correct
    # ◀ Older N/N counter on the artifact the user looks at most.
    if sess.window_id:
        try:
            from .history import prewarm_pages_cache

            await prewarm_pages_cache(sess.window_id)
        except Exception as e:
            logger.debug("finalize_task prewarm failed: %s", e)

    # Final answer → ``is_busy=False`` keyboard so the user sees Kill,
    # not Stop. State stays live (rolling card); the next turn's events
    # keep editing the SAME message — no reset_card, no pin.
    done_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)

    text = _render_card(sess, state, user_id=user_id)
    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text, reply_markup=done_kb)
    elif await _edit_card(bot, user_id, state, text=text, reply_markup=done_kb):
        state.last_rendered = text
    state.last_edit_ts = time.monotonic()

    if attachments:
        await _send_attachments(bot, user_id, attachments)


async def _send_attachments(
    bot: Bot, user_id: int, attachments: list[Attachment]
) -> None:
    """Send extracted overflow content. ``kind="photo"`` table extracts
    are rasterised via ``screenshot.text_to_image`` so wide tables land
    as inline images rather than `.md` files; everything else (oversized
    code blocks) goes through ``send_document`` as before.
    """
    import io as _io

    from ..screenshot import text_to_image
    from .tg_format import pretty_pad_table

    for att in attachments:
        try:
            if att.kind == "photo":
                source = att.content.decode("utf-8", errors="replace")
                rendered = pretty_pad_table(source)
                png = await text_to_image(rendered, with_ansi=False)
                await bot.send_photo(
                    chat_id=user_id,
                    photo=_io.BytesIO(png),
                )
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=_io.BytesIO(att.content),
                    filename=att.filename,
                )
        except Exception as e:
            logger.debug("attachment %s send failed: %s", att.filename, e)


async def push_event(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    text: str,
    is_error: bool = False,
) -> None:
    """C5 push notification — separate `send_message` for one of the
    user-approved triggers (completion / blocker error / interactive UI /
    lifecycle / inbox / quota).
    """
    emoji = "🟥" if is_error else session_emoji(sess)
    body = f"{emoji} \\[{sess.name or sess.id}\\] {text}"
    if len(body) > 3500:
        body = body[:3497] + "…"
    try:
        sent = await safe_send(bot, user_id, body)
    except Exception as e:
        logger.debug("push_event failed: %s", e)
        return
    # Migrate the switcher onto the latest pushed message. The keyboard's
    # busy state mirrors whether the session has a live card right now —
    # finalize_task / completion-style pushes happen after reset_card so
    # the live state is gone, the user is at idle and should see Kill.
    if sent is not None:
        _register_msg(user_id, sent.message_id, sess.id)
        live = _cards.get((user_id, sess.id))
        is_busy = live is not None and live.msg_id is not None
        keyboard: InlineKeyboardMarkup | None = build_footer_keyboard(
            user_id, screen="main", is_busy=is_busy
        )
        if keyboard is not None:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=user_id, message_id=sent.message_id, reply_markup=keyboard
                )
                prev = session_manager.get_last_switcher_msg(user_id)
                if prev and prev != sent.message_id:
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=user_id, message_id=prev, reply_markup=None
                        )
                    except Exception:
                        pass
                session_manager.set_last_switcher_msg(user_id, sent.message_id)
            except Exception as e:
                logger.debug("push_event switcher migrate failed: %s", e)


def is_card_busy(user_id: int, session_id: str) -> bool:
    """True when the user's live card for ``session_id`` is currently in
    flight AND visible (msg_id set, finalize_task hasn't run, and the
    card is not paused for menu navigation). Used by the polling-based
    typing-indicator path — TYPING should fire while a turn is mid-
    stream during silent gaps between events, but NOT while the user
    is browsing the inline ⋯ Menu / sub-screens. While ``in_menu_view``
    the card buffers events without rendering to chat, so a "typing…"
    indicator there is just noise.
    """
    state = _cards.get((user_id, session_id))
    if state is None or state.in_menu_view:
        return False
    return _card_is_busy(state)


def is_active_for_user(user_id: int, sess: Session) -> bool:
    active = session_manager.get_active_session(user_id)
    return active is not None and active.id == sess.id


async def repost_card(bot: Bot, user_id: int, sess: Session) -> None:
    """Send a fresh live-card below the user's latest message, and drop
    the previous one if it exists.

    Called from text_handler when ``card_position == repost`` so the
    user always sees a bot-side card immediately below the message they
    just typed (instead of having to wait for claude's first event,
    which may come seconds later — long enough that the user reports
    "I don't see updates").

    No-op only when the card is paused (Menu / sub-screen open). In all
    other cases — including the post-finalize_task state where the
    previous live card was already pinned + reset — we seed a fresh
    card so it lands below the user's typed line. When claude's first
    event arrives it will edit *this* card (state.msg_id is now set)
    instead of spawning a separate one above the user msg.
    """
    state = _cards.get((user_id, sess.id))
    if state is not None and state.in_menu_view:
        return
    state = get_card_state(user_id, sess)
    # Seed history from JSONL on first call after a bot restart so the
    # reposted card lands with full context, not an empty body.
    await _ensure_seeded(user_id, sess, state)
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
        state.pending_edit = None

    old_msg_id = state.msg_id
    state.msg_id = None  # force _send_card to create a fresh message

    text = _render_card(sess, state, user_id=user_id)
    await _send_card(bot, user_id, sess, state, text=text)
    state.last_rendered = text
    state.last_edit_ts = time.monotonic()
    if old_msg_id and state.msg_id and state.msg_id != old_msg_id:
        try:
            await bot.delete_message(chat_id=user_id, message_id=old_msg_id)
        except Exception as e:
            logger.debug("repost_card delete old failed: %s", e)


async def refresh_panel(bot: Bot, user_id: int) -> None:
    """Re-render the active session's live card so the bg-status panel
    (and active quota glyph) reflects the latest bg_status state.

    No-op when:
      - the user has no active session
      - the active session has no live card yet
      - the card is paused (menu/sub-screen open)
      - a deferred edit is already queued — that edit will pick up the
        latest panel state on its own when it fires
    """
    active = session_manager.get_active_session(user_id)
    if active is None:
        return
    state = _cards.get((user_id, active.id))
    if state is None or state.msg_id is None or state.in_menu_view:
        return
    if state.pending_edit is not None and not state.pending_edit.done():
        return
    text = _render_card(active, state, user_id=user_id)
    if text == state.last_rendered:
        return
    if await _edit_card(bot, user_id, state, text=text):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()


# ─── Tool-timer tick ──────────────────────────────────────────────────

# How often to re-render the active card to advance the ⏳ M:SS counter
# on the latest in-flight tool/thinking entry. 3–4 s is the sweet spot:
# fast enough to feel live, slow enough that we don't waste editMessageText
# calls or hit the 30/s rate limiter.
CARD_TIMER_TICK_SECONDS = 3.0


def _latest_inflight_idx(page_events: list[Event]) -> int | None:
    """Index of the last in-flight event on a page, or None if none."""
    for i in range(len(page_events) - 1, -1, -1):
        if _is_in_flight(page_events[i], page_events, i):
            return i
    return None


async def card_timer_loop(bot: Bot) -> None:
    """Background task that ticks the elapsed timer on the latest
    in-flight tool/thinking entry of each user's active card.

    Skips:
      - cards with no msg_id
      - paused cards (in_menu_view)
      - users whose pagination puts them on a non-latest page (timer
        only ticks on the page where the in-flight event lives, i.e.
        the latest page)
      - cards with a pending deferred edit (the deferred edit picks up
        the updated timer when it fires)
    """
    logger.info("card_timer_loop started tick=%.1fs", CARD_TIMER_TICK_SECONDS)
    while True:
        try:
            await asyncio.sleep(CARD_TIMER_TICK_SECONDS)
            for (uid, sid), state in list(_cards.items()):
                try:
                    if state.msg_id is None or state.in_menu_view:
                        continue
                    sess = session_manager.get_session(sid)
                    if sess is None:
                        continue
                    # Only the user's currently-active session ticks.
                    active = session_manager.get_active_session(uid)
                    if active is None or active.id != sid:
                        continue
                    pages = paginate_events(state.events)
                    idx = _resolved_page_idx(state, len(pages))
                    # Timer renders only on the latest page.
                    if idx != len(pages) - 1:
                        continue
                    if _latest_inflight_idx(pages[idx]) is None:
                        continue
                    # Skip when an edit is already queued — it'll pick
                    # up the fresh timer value when it fires.
                    if state.pending_edit is not None and not state.pending_edit.done():
                        continue
                    text = _render_card(sess, state, user_id=uid)
                    if text == state.last_rendered:
                        continue
                    if await _edit_card(bot, uid, state, text=text):
                        state.last_rendered = text
                        state.last_edit_ts = time.monotonic()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.debug("card_timer tick failed for sess=%s: %s", sid, e)
        except asyncio.CancelledError:
            logger.info("card_timer_loop cancelled")
            break
        except Exception as e:
            logger.warning("card_timer_loop error: %s", e)
