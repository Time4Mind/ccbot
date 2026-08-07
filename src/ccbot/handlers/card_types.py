"""State-only dataclasses and constants for live cards."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

__all__ = [
    "CARD_HARD_LIMIT",
    "CARD_MAX_EVENTS",
    "STALE_CARD_SECONDS",
    "SPOILER_MAX_LINES",
    "CARD_PAGE_BUDGET",
    "CARD_PAGE_LINES_DEFAULT",
    "CARD_PAGE_LINES_OVERSHOOT",
    "CARD_SEED_TURNS",
    "Event",
    "CardState",
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
    pending_edit_in_flight: bool = False  # Telegram request phase of pending_edit
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
    # Inline-screenshots mode (Task #48). On Bot API versions with rich
    # media support, the pane render is the final media block of the Rich
    # Markdown card. ``is_photo_msg`` remains the compatibility fallback
    # for older Bot API servers / rich-disabled deployments.
    is_rich_media_msg: bool = False
    rich_media_file_id: str = ""
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
    # Transcript mtime (epoch seconds) at the last *empty* seed attempt, or
    # -1.0 if never attempted. A freshly restored (``claude --resume``)
    # session creates its card before claude has flushed the resumed
    # transcript, so an early seed reads [] and must retry on a later event.
    # ``_ensure_seeded`` only re-parses the (possibly multi-MB) JSONL once
    # this advances, so a burst of events during the resume window costs one
    # stat() each, not a full re-parse. Reset alongside ``seed_attempted``
    # at the non-destructive re-seed sites.
    seed_mtime: float = -1.0
    # Stall-recovery flag. Set by ``maybe_finalize_stalled`` after it
    # appends the STALL_NOTE final_text. If the stall was a false positive
    # (a genuine assistant turn arrives after), the next
    # ``update_session_card`` / ``finalize_task`` wipes the card binding
    # and lets ``_send_card`` spawn a fresh message below the stalled
    # stub — so the recovered answer is visible instead of being silently
    # edited into a card the user has scrolled past or marked complete.
    stall_finalized: bool = False
    # Set by voice_handler right when a voice message is pinned to this
    # session, before download/transcribe (which can take many seconds).
    # Rendered as a synthetic trailing ``user_msg`` row so an immediate
    # repost_card shows "yes, your voice landed here" in the same place
    # typed prompts appear, rather than adding transient state to the card
    # header. Cleared once the transcribed text is actually dispatched (or
    # transcription fails).
    voice_pending: bool = False
