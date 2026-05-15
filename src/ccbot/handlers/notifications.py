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
CARD_MAX_EVENTS = 200
# After this much idleness, the next event opens a fresh card.
STALE_CARD_SECONDS = 5 * 60
# Max lines of body shown inside each tool/thinking spoiler. Overflow is
# truncated with a "… (+N more lines)" trailer. Env-tunable.
SPOILER_MAX_LINES = 5

# Char budget for one rendered card page. Telegram message limit is 4096;
# we leave headroom for header, divider, bg-panel.
CARD_PAGE_BUDGET = 3500

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

# Per-user set of message_ids that hold a finalised live-card body (the
# task's final answer). Any callback-driven edit on such a message
# would clobber an immutable chat artifact, so ``safe_edit`` redirects
# to a fresh message and strips the keyboard from the finalised one.
# In-memory only — resets across bot restarts (acceptable: the user
# would re-establish a fresh carrier on the next event anyway).
_finalized_msgs: dict[int, set[int]] = {}
_FINALIZED_LIMIT_PER_USER = 200


def mark_msg_finalized(user_id: int, message_id: int) -> None:
    """Pin a Telegram message as a finalised live card so UI navigation
    spawns a new carrier instead of overwriting the answer.

    Capped per user to keep memory bounded — oldest ids drop first.
    """
    bucket = _finalized_msgs.setdefault(user_id, set())
    if len(bucket) >= _FINALIZED_LIMIT_PER_USER and message_id not in bucket:
        drop = max(1, _FINALIZED_LIMIT_PER_USER // 10)
        for _ in range(drop):
            try:
                bucket.pop()
            except KeyError:
                break
    bucket.add(message_id)


def is_msg_finalized(user_id: int, message_id: int) -> bool:
    return message_id in _finalized_msgs.get(user_id, ())


def discard_finalized_msg(user_id: int, message_id: int) -> None:
    bucket = _finalized_msgs.get(user_id)
    if bucket:
        bucket.discard(message_id)
        if not bucket:
            _finalized_msgs.pop(user_id, None)


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
# MarkdownV2 markers we strip for plain-text card rendering. The card is
# sent without ``parse_mode``, so ``**bold**`` would otherwise show as
# literal asterisks. (History view still gets full markdown via
# ``safe_edit`` / ``_ensure_formatted`` — this only flattens the card.)
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITALIC_RE = re.compile(r"(?<!\w)_(.+?)_(?!\w)", re.DOTALL)
_MD_BACKTICK_RE = re.compile(r"`+")
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
    """Strip markup so the card renders cleanly as plain text.

    The live card is sent without ``parse_mode`` — Markdown markers and
    expandable-quote sentinels would otherwise show up as literal
    characters in chat. Strip:

    * The full ``EXPQUOTE_START … EXPQUOTE_END`` block (one-liner heads
      that historically embedded a body block — body lives in
      ``event.body`` now, not in ``event.text``).
    * Any leftover lone ``EXPQUOTE_*`` sentinel that snuck through
      (transcript_format nests these in tool body strings).
    * ``**bold**`` → ``bold``, ``_italic_`` → ``italic``, backticks
      dropped.
    * ``$HOME`` → ``~`` so long Mac paths don't waste 30+ chars.
    """
    import os

    out = _EXPQUOTE_BLOCK_RE.sub("", text)
    out = _EXPQUOTE_ANY_RE.sub("", out)
    out = _MD_BOLD_RE.sub(r"\1", out)
    out = _MD_ITALIC_RE.sub(r"\1", out)
    out = _MD_BACKTICK_RE.sub("", out)
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


def _split_tool_text(raw: str) -> tuple[str, str, str]:
    """Split transcript_format's tool text into head / summary / content.

    ``raw`` reaches us in the shape::

        **ToolName**(args)
          ⎿  Output N lines
        \\x02EXPQUOTE_START\\x02<content>\\x02EXPQUOTE_END\\x02

    Returns ``(head, summary, content)`` where:

    * ``head``    = first line, markdown-stripped (e.g. ``Bash(args)``)
    * ``summary`` = second line stripped of the ``⎿`` glyph and indent
      (e.g. ``Output 5 lines``), or "" when absent
    * ``content`` = everything inside the EXPQUOTE block, or "" when
      there isn't one. The duplicate head/summary that transcript_parser
      often re-embeds inside the quote is filtered out so the card
      doesn't show ``Tool(args)`` twice.
    """
    if not raw:
        return "", "", ""
    parts = raw.split("\n", 2)
    head = _strip_for_card(parts[0]) if parts else ""
    summary = ""
    rest = ""
    if len(parts) >= 2:
        summary_line = parts[1].lstrip(" ").lstrip("⎿").strip()
        summary = _strip_for_card(summary_line)
    if len(parts) >= 3:
        rest = parts[2]
    # Extract inner content from EXPQUOTE block if present, otherwise
    # treat ``rest`` as plain content.
    inner = _extract_expquote_inner(rest) if rest else ""
    content = inner if inner else rest
    # Drop the duplicate head row that transcript_parser sometimes
    # re-embeds at the top of the EXPQUOTE block.
    if content:
        content_lines = content.split("\n")
        # Strip if first content line matches head (with or without ✓/▷)
        first_norm = _strip_for_card(content_lines[0]).strip()
        head_norm = head.strip()
        if (
            first_norm == head_norm
            or first_norm.endswith(head_norm)
            or first_norm.startswith(("✓ ", "▷ ", "✗ "))
            and head_norm in first_norm
        ):
            content_lines = content_lines[1:]
        # Drop the trailing/leading "⎿  summary" repeat too.
        if content_lines and content_lines[0].lstrip().startswith("⎿"):
            content_lines = content_lines[1:]
        content = "\n".join(content_lines).strip("\n")
    return head, summary, content


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
        head, _summary, content = _split_tool_text(raw_body)
        return Event(
            type="tool_use",
            text=_trim(head, 160),
            body=content,
            started_at=started,
            tool_use_id=msg.tool_use_id,
            tool_name=msg.tool_name,
        )
    if msg.content_type == "tool_result":
        head, summary, content = _split_tool_text(raw_body)
        # On a tool_result, prefer head+summary in ``text`` so a missed
        # fold (no matching tool_use Event) still displays a useful
        # one-liner. ``_apply_tool_result`` reads these into the
        # original Event.
        if summary:
            head_with_summary = f"{head} · {summary}" if head else summary
        else:
            head_with_summary = head
        return Event(
            type="tool_result",
            text=_trim(head_with_summary, 200),
            body=content,
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


def _chunk_final_text(text: str, budget: int = CARD_PAGE_BUDGET) -> list[str]:
    """Split a long final answer into chunks each ≤ ``budget`` chars.

    Tries to break on paragraph boundaries (``\\n\\n``), then line
    boundaries (``\\n``), then hard char cut as a last resort. Each chunk
    becomes a separate ``final_text`` Event with ``is_page_break=True``,
    so the card renders one chunk per page and never overflows
    Telegram's 4096-char message limit.

    Empty / short input returns a single-chunk list (or ``[]`` when empty).
    """
    if not text:
        return []
    if len(text) <= budget:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > budget:
        # Prefer paragraph break.
        cut = remaining.rfind("\n\n", 0, budget)
        if cut <= 0:
            # Try line break.
            cut = remaining.rfind("\n", 0, budget)
        if cut <= 0:
            # Hard cut.
            cut = budget
        chunk = remaining[:cut].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


def _indent_body(body: str) -> str:
    """Format ``body`` as a plain-text indented block under a head.

    The card is sent without ``parse_mode``, so we can't use MarkdownV2
    expandable blockquotes — they'd render as literal ``**>…||``. Indent
    body lines with two spaces, strip markdown markers, cap at
    ``SPOILER_MAX_LINES``.
    """
    trimmed = _body_trim(_strip_for_card(body))
    if not trimmed:
        return ""
    return "\n".join(f"  {line}" for line in trimmed.split("\n"))


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
        body = _indent_body(event.body)
        return f"{head}\n{body}" if body else head

    if event.type == "tool_use":
        if event.is_error:
            glyph = "✗"
        elif in_flight:
            glyph = "▷"
        else:
            glyph = "✓"
        head = f"{glyph} {event.text}{marker}"
        body = _indent_body(event.body)
        return f"{head}\n{body}" if body else head

    if event.type == "tool_result":
        # Fallback when the matching tool_use Event isn't found (parser
        # race / restart). Render as a standalone row.
        head = f"✓ {event.text}{marker}"
        body = _indent_body(event.body)
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
    """Render the events of one page into a single body string."""
    parts: list[str] = []
    for i, ev in enumerate(events):
        parts.append(render_event(ev, in_flight=_is_in_flight(ev, events, i), now=now))
    return "\n".join(parts)


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
    # Active-session quota glyph in the header — ⚠️🟢/🟡/🔴 when the
    # session has crossed a usage threshold. Same data source as the
    # bg-panel rows for non-active sessions.
    quota_glyph = ""
    if user_id is not None:
        quota_glyph = bg_status.quota_glyph_for(user_id, sess.id)
    # Last-event timestamp — HH:MM of the most recent event of any kind.
    ts_suffix = ""
    if state.last_event_ts > 0:
        ts_suffix = " · " + _format_hhmm(state.last_event_ts)
    name_part = sess.name or sess.id
    if quota_glyph:
        header = (
            f"{emoji} *{name_part}* {quota_glyph} · "
            f"{state_label}{cont_marker}{ts_suffix}"
        )
    else:
        header = f"{emoji} *{name_part}* · {state_label}{cont_marker}{ts_suffix}"
    if sess.goal:
        header += f"\ngoal: {sess.goal}"

    pages = paginate_events(state.events)
    idx = _resolved_page_idx(state, len(pages))
    body = render_page(pages[idx], now=time.time())

    # Optional bg-panel always lives at the bottom.
    panel = ""
    if user_id is not None:
        panel = bg_status.render_panel(user_id, active_session_id=sess.id)

    # Enforce the 4096-char Telegram limit at render time. Header +
    # divider + footer + panel take a fixed prefix budget; whatever
    # remains is for body. If body overflows, drop the OLDEST lines on
    # the page (keep the freshest signal — the in-flight tool, the
    # final-text chunk, the latest narration). Without this guard the
    # ``Message_too_long`` BadRequest fires, ``_edit_card`` returns
    # False, and the fallback path posts a NEW card → duplicate
    # messages stack up in chat.
    prefix = header + "\n─────\n"
    suffix = ""
    if footer:
        suffix += "\n─────\n" + footer
    if panel:
        suffix += "\n" + panel
    budget = 4096 - len(prefix) - len(suffix) - 32  # safety margin
    if body and len(body) > budget:
        body = _trim_body_from_top(body, budget)

    parts = [header, "─────"]
    if body:
        parts.append(body)
    if footer:
        parts.append("─────")
        parts.append(footer)
    if panel:
        parts.append(panel)
    return "\n".join(parts)


def _trim_body_from_top(body: str, budget: int) -> str:
    """Drop oldest lines from ``body`` until total length ≤ ``budget``.

    The card's "latest signal" lives at the bottom (newest event), so we
    preserve the tail and trim the head. A ``…`` marker indicates the
    truncation point so the user knows there's hidden context above.
    """
    if len(body) <= budget:
        return body
    lines = body.split("\n")
    # Walk from the end, accumulating until we hit budget.
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        line_len = len(line) + 1  # +1 for the joining newline
        if total + line_len > budget:
            break
        kept.append(line)
        total += line_len
    kept.reverse()
    if not kept:
        return body[-budget:]
    if len(kept) < len(lines):
        kept.insert(0, "… (older events on previous pages)")
    return "\n".join(kept)


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


async def _seed_events_from_jsonl(sess: Session) -> list[Event]:
    """Build a list[Event] from the session's JSONL transcript.

    Pulls the last ``CARD_SEED_TURNS`` end-of-turn boundaries so the
    card has visible history after a bot restart (when in-memory
    ``state.events`` is empty). Returns ``[]`` on any failure — caller
    just continues with an empty card.
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
            and getattr(p, "stop_reason", "") in ("end_turn", "stop_sequence", "max_tokens")
        ):
            end_turn_idxs.append(i)
            if len(end_turn_idxs) >= CARD_SEED_TURNS:
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
    seeded = await _seed_events_from_jsonl(sess)
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
    """Re-render the card with whatever events accumulated while paused,
    then drop the pause."""
    state = _cards.get((user_id, sess.id))
    if state is None or state.msg_id is None:
        return
    state.in_menu_view = False
    if state.pending_edit is not None and not state.pending_edit.done():
        state.pending_edit.cancel()
    state.pending_edit = None
    text = _render_card(sess, state, user_id=user_id)
    keyboard = build_footer_keyboard(user_id, screen="main", is_busy=True)
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
            reply_markup=keyboard,
        )
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    except Exception as e:
        logger.debug("resume_card_view edit failed: %s", e)


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
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=carrier_msg_id,
            text=text,
            reply_markup=keyboard,
        )
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
    except Exception as e:
        logger.debug("paint_card_on_carrier edit failed: %s", e)


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
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
            reply_markup=cleared_kb,
        )
    except Exception as e:
        logger.debug("clear_card edit failed: %s", e)
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
    try:
        sent = await bot.send_message(
            chat_id=user_id,
            text=text,
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
    try:
        await bot.edit_message_text(
            chat_id=user_id,
            message_id=state.msg_id,
            text=text,
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
            # opens a fresh card. Other BadRequests (e.g. parse errors,
            # Message_too_long) keep the existing msg_id; the caller
            # logs a warning and the next render retries on the same
            # message.
            logger.info(
                "card edit lost-carrier msg_id=%s err=%s", state.msg_id, err
            )
            state.msg_id = None
            return False
        logger.warning("card edit failed (BadRequest): %s", err)
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
    # ``CARD_PAGE_BUDGET`` chars (well below Telegram's 4096 limit) so
    # the rendered page never overflows. Default focus lands on the
    # FIRST chunk's page so the user reads the answer from the top.
    now = time.time()
    stripped_full = _strip_for_card(cleaned)
    chunks = _chunk_final_text(stripped_full)
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

    # Finalised card → ``is_busy=False`` keyboard so the user sees Kill,
    # not Stop, on a completed result.
    done_kb = build_footer_keyboard(user_id, screen="main", is_busy=False)

    # Track every Telegram message this finalize fans out into so each
    # gets pinned in ``_finalized_msgs`` below. Without that pinning, the
    # next UI callback on the answer card would clobber the answer via
    # ``safe_edit``. The pin reroutes any such edit to a fresh carrier.
    finalised_ids: list[int] = []

    text = _render_card(sess, state, user_id=user_id)
    if state.msg_id is None:
        await _send_card(bot, user_id, sess, state, text=text, reply_markup=done_kb)
    else:
        if await _edit_card(bot, user_id, state, text=text, reply_markup=done_kb):
            state.last_rendered = text
    state.last_edit_ts = time.monotonic()
    if state.msg_id is not None:
        finalised_ids.append(state.msg_id)
    # ``chunks`` retained for future overflow split if a single answer
    # exceeds 4096 chars after pagination — pagination handles this by
    # treating the spilled chunk as a sub-page. Today's path renders all
    # of cleaned into the final_event.body and trusts pagination to split.
    del chunks

    if attachments:
        await _send_attachments(bot, user_id, attachments)

    for mid in finalised_ids:
        mark_msg_finalized(user_id, mid)

    # No separate "✓ done" push under the card — the result is already
    # rendered inside the card body. Errors and AskUserQuestion still
    # emit pushes (those need to ping the user).
    reset_card(user_id, sess.id)


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
