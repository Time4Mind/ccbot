"""Compose the complete live-card body, prompt view, footer, and status panel."""

from __future__ import annotations

import re
import time

from ..i18n import t
from ..session import Session
from . import bg_status
from .card_budget import _format_hhmmss
from .card_event_render import render_event
from .card_pagination import (
    _EVENT_JOINER,
    _rechunk_oversized_finals_inplace,
    _resolve_line_budget,
    _resolved_page_idx,
    _trim_page_events,
    paginate_events_for_card,
    render_page,
)
from .card_types import CardState, Event
from .switcher import session_emoji

__all__ = [
    "_BOX_DRAWING_RE",
    "_BORDER_ONLY_LINE_RE",
    "_BOX_FRAME_RE",
    "_NUMBERED_OPTION_RE",
    "_RULE_LINE_RE",
    "_KB_BLOCK_SEPARATOR",
    "_KB_PARAGRAPH_SEPARATOR",
    "_KB_HARD_BREAK_JOIN",
    "_format_kb_prompt",
    "_rule_between_options",
    "_sanitize_prompt_block",
    "_render_card",
]

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

# Numbered-option row in an AskUserQuestion / picker pane. Tolerates the
# common cursor markers (``> `` / ``❯ ``) and arbitrary leading whitespace
# (Claude Code uses ``  2.`` to align the non-cursor rows under the
# cursor row). Anchors to the start of the line so prose like
# ``Step 1.`` never trips it.
_NUMBERED_OPTION_RE = re.compile(r"^\s*[>❯▶]?\s*\d+\.\s")

# Source-level horizontal rule the generated ``─────`` separators
# already cover. Drop these during formatting so a verbatim divider
# line in the pane (e.g. NORMAL_PROMPT in the regression tests) doesn't
# show up as ``─────  ─────  ─────`` once we add ours.
_RULE_LINE_RE = re.compile(r"^[─\-=_]{3,}$")


_KB_BLOCK_SEPARATOR = "\n\n─────\n\n"
_KB_PARAGRAPH_SEPARATOR = "\n\n"
_KB_HARD_BREAK_JOIN = "  \n"


def _format_kb_prompt(raw: str) -> str:
    """Render a captured AskUserQuestion / picker pane as kb-mode body.

    Numbered options (``1. Foo``, ``❯ 2. Bar``) become their own blocks
    separated by a ``─────`` rule — same archive-style split used in
    ``handlers/archive.py`` for the session list. The header / hint
    chrome around the options keeps a hard-break join so wrapping prose
    stays readable on the phone.

    Pure prompts with no numbered options (ExitPlanMode, plain
    confirmations) fall back to the cheap paragraph+hard-break join —
    no spurious dividers.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []

    def _flush() -> None:
        if current:
            paragraphs.append(current.copy())
            current.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            _flush()
            continue
        if _RULE_LINE_RE.match(stripped):
            # Source rule lines are absorbed by the generated dividers
            # — surviving as content would double up the separator.
            _flush()
            continue
        current.append(line)
    _flush()

    if not paragraphs:
        return ""

    # Re-split each paragraph at numbered-option boundaries so every
    # option ends up as its own block (and gets its own ─── divider).
    refined: list[list[str]] = []
    for para in paragraphs:
        buf: list[str] = []
        for line in para:
            if _NUMBERED_OPTION_RE.match(line):
                if buf:
                    refined.append(buf)
                    buf = []
                refined.append([line])
            else:
                buf.append(line)
        if buf:
            refined.append(buf)

    has_options = any(_NUMBERED_OPTION_RE.match(p[0]) for p in refined if p)
    sep = _KB_BLOCK_SEPARATOR if has_options else _KB_PARAGRAPH_SEPARATOR
    return sep.join(_KB_HARD_BREAK_JOIN.join(p) for p in refined)


def _rule_between_options(body: str) -> str:
    """Splice a ``─────`` rule before each numbered option (after the first).

    Used by the box-frame branch of ``_render_card``. That branch renders
    the de-framed prompt inside a code fence, which suppresses MarkdownV2 —
    so options can't be separated by markup the way the frameless path does.
    Instead we splice literal ``─────`` rule lines between options; inside
    the fence they render as plain monospace dividers, giving the same
    archive-style separation without re-introducing the blockquote-collapse
    the fence exists to prevent.

    Each option keeps its trailing preview/description lines (they ride with
    the option until the next numbered row). Pre-existing source rule lines
    are dropped so separators never double up.
    """
    out: list[str] = []
    seen_option = False
    for line in body.splitlines():
        if _RULE_LINE_RE.match(line.strip()):
            continue  # absorbed by the generated rules
        if _NUMBERED_OPTION_RE.match(line):
            if seen_option:
                out.append("─────")
            seen_option = True
        out.append(line)
    return "\n".join(out)


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
    state_label = sess.dir_label if sess.state == "active" else sess.state
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
            # Splice ───── rules between numbered options so they're visibly
            # separated inside the fence (which suppresses MarkdownV2, so the
            # frameless path's markup dividers can't apply here).
            body = _rule_between_options(body)
            prompt_part = body if "```" in body else f"```\n{body}\n```"
        else:
            # Format pane lines into explicit blocks: each numbered
            # option ("1. Foo", "❯ 2. Bar") gets its own ─── divider
            # — same archive-style split as ``handlers/archive.py``'s
            # session list. Non-option prompts (ExitPlanMode and the
            # like) fall back to hard-break-only join so wrapping
            # prose stays one paragraph. See ``_format_kb_prompt``.
            prompt_part = _format_kb_prompt(raw)
        parts = [header, "─────", "⌨ *Waiting for your input:*", prompt_part]
        # Paragraph-break join (same trap the bottom of this function
        # already handles): single ``\n`` between header / separator /
        # title would let the rich parser glue them onto one line.
        return "\n\n".join(parts)

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
    if state.voice_pending:
        pending_row = render_event(
            Event(
                type="user_msg",
                text=t(user_id or 0, "voice.transcribing"),
                started_at=time.time(),
            ),
            in_flight=False,
            now=time.time(),
        )
        body = _EVENT_JOINER.join(part for part in (body, pending_row) if part)

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
        # Same `` ``-paragraph trick used by ``_EVENT_JOINER``:
        # CommonMark collapses consecutive blank lines into one
        # paragraph break, but a paragraph that contains a
        # non-breaking space survives — visibly DOUBLES the gap above
        # the ``context: N%`` row so it doesn't read glued onto the
        # last body event.
        parts.append(" ")
        parts.append(f"context: {state.context_pct}%")
    if panel:
        # The panel carries its own ``─── фон ───`` label-separator
        # (pivot #39 feedback: previously the bg-row glued to the last
        # body line). Same nbsp-paragraph trick to widen the gap.
        parts.append(" ")
        parts.append(panel)
    # Paragraph-break join (``\n\n``) — single ``\n`` is a CommonMark
    # soft break that the rich parser collapses to a space, glueing
    # ``header ───── body ───── footer`` onto one row instead of each
    # on its own line. Same trap we hit in /archive and the bg-panel.
    return "\n\n".join(parts)
