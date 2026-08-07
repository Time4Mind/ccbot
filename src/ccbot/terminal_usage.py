"""Parse Claude Code's ``/usage`` terminal modal.

This leaf module owns usage-modal data models, row extraction, percentage and
reset-time parsing. ``ccbot.terminal_parser`` re-exports its historical API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "UsageInfo",
    "UsageBreakdown",
    "_parse_clock_to_24h",
    "_parse_pct",
    "extract_usage_breakdown",
    "parse_usage_output",
]


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]


@dataclass
class UsageBreakdown:
    """Structured extract of the three usage rows + extra-usage flag.

    Each `pct` is the percentage Claude reports as "used"; `reset_hhmm` is
    the wall-clock reset time in 24h format ("HH:MM"). Either may be None
    if the row was missing or malformed in the captured pane.
    """

    session_pct: int | None = None
    session_reset_hhmm: str | None = None
    week_pct: int | None = None
    week_reset_hhmm: str | None = None
    week_sonnet_pct: int | None = None
    week_sonnet_reset_hhmm: str | None = None
    extra_enabled: bool = False


def _parse_clock_to_24h(text: str) -> str | None:
    """Parse strings like ``9:59pm``, ``4pm``, ``May 17 at 4pm`` → ``HH:MM``.

    Claude Code's ``/usage`` modal switched, around mid-week, from
    ``Resets 4pm (Europe/Moscow)`` to ``Resets May 17 at 4pm
    (Europe/Moscow)`` on the *Current week* rows. Use ``re.search``
    (not ``re.match``) so the time can appear anywhere in the string,
    and accept an optional ``at`` separator.
    """
    m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text, re.IGNORECASE)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2)) if m.group(2) else 0
    ampm = m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour < 24 and 0 <= minute < 60):
        return None
    return f"{hour:02d}:{minute:02d}"


def _parse_pct(text: str) -> int | None:
    m = re.search(r"(\d+)\s*%\s*used", text)
    return int(m.group(1)) if m else None


def extract_usage_breakdown(info: UsageInfo) -> UsageBreakdown:
    """Walk parsed_lines, looking for the three section headers and
    pulling the percentage + reset time + extra flag out of each.
    """
    out = UsageBreakdown()
    state: str | None = None
    for raw in info.parsed_lines:
        s = raw.strip()
        if "Current session" in s:
            state = "session"
            continue
        if "Current week" in s and "all models" in s.lower():
            state = "week_all"
            continue
        if "Current week" in s and "Sonnet" in s:
            state = "week_sonnet"
            continue
        if s.startswith("Extra usage"):
            state = "extra"
            # The label itself sometimes lives on its own line; the value
            # follows. Don't reset state — pick up "not enabled" / "enabled"
            # below.
            continue

        if state == "session":
            pct = _parse_pct(s)
            if pct is not None:
                out.session_pct = pct
            elif s.lower().startswith("resets"):
                out.session_reset_hhmm = _parse_clock_to_24h(
                    re.sub(r"^resets\s*", "", s, flags=re.IGNORECASE)
                )
        elif state == "week_all":
            pct = _parse_pct(s)
            if pct is not None:
                out.week_pct = pct
            elif s.lower().startswith("resets"):
                out.week_reset_hhmm = _parse_clock_to_24h(
                    re.sub(r"^resets\s*", "", s, flags=re.IGNORECASE)
                )
        elif state == "week_sonnet":
            pct = _parse_pct(s)
            if pct is not None:
                out.week_sonnet_pct = pct
            elif s.lower().startswith("resets"):
                out.week_sonnet_reset_hhmm = _parse_clock_to_24h(
                    re.sub(r"^resets\s*", "", s, flags=re.IGNORECASE)
                )
        elif state == "extra":
            low = s.lower()
            if "not enabled" in low:
                out.extra_enabled = False
            elif "enabled" in low:
                out.extra_enabled = True
    return out


def parse_usage_output(pane_text: str) -> UsageInfo | None:
    """Extract usage information from Claude Code's /usage settings tab.

    Three start signals, tried in order:

    * modern tabs row ``Status  Config  Usage  Stats``,
    * legacy header ``Settings: ... Usage``,
    * body fallback — any ``Current session`` / ``Current week`` line.

    The last one matters because ``tmux capture-pane`` reads only the
    visible viewport (no scrollback by default). On a narrow pane the
    modal body is taller than the visible rows, the tabs row scrolls
    above the top, and the header-only detection returns ``None`` even
    though every usage row is right there in the capture. The fallback
    catches exactly that case.

    Returns ``UsageInfo`` with cleaned lines, or ``None`` if neither
    signal is present.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")

    start_idx: int | None = None
    end_idx: int | None = None

    # Pass 1: header-based detection. The modal can appear multiple
    # times in a scrollback capture (each /usage attempt leaves its
    # transcript behind), so walk backwards and pick the LAST header
    # — that's the freshest modal, the one matching the data we want.
    header_positions: list[int] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        is_modern = (
            "Status" in stripped
            and "Config" in stripped
            and "Usage" in stripped
            and "Stats" in stripped
        )
        is_legacy = "Settings:" in stripped and "Usage" in stripped
        if is_modern or is_legacy:
            header_positions.append(i)
    if header_positions:
        start_idx = header_positions[-1] + 1
        for j in range(start_idx, len(lines)):
            if lines[j].strip().startswith("Esc to"):
                end_idx = j
                break

    # Pass 2: header escaped the captured viewport. Anchor on the LAST
    # "Current session" line — that's the earliest body marker of the
    # freshest modal, so we still pick up "Current week (all models)"
    # below it. Falling back to the last "Current week" only when no
    # session row was captured.
    if start_idx is None:
        session_positions = [i for i, ln in enumerate(lines) if "Current session" in ln]
        week_positions = [i for i, ln in enumerate(lines) if "Current week" in ln]
        if session_positions:
            start_idx = session_positions[-1]
        elif week_positions:
            start_idx = week_positions[-1]
        else:
            return None
        # Look for the dismiss sentinel in the remainder; if it's gone
        # too (long modal on a tiny pane) we keep everything.
        for j in range(start_idx, len(lines)):
            if lines[j].strip().startswith("Esc to"):
                end_idx = j
                break

    if end_idx is None:
        end_idx = len(lines)

    # Collect content lines, stripping progress bar characters and whitespace
    cleaned: list[str] = []
    for line in lines[start_idx:end_idx]:
        # Strip the line but preserve meaningful content
        stripped = line.strip()
        if not stripped:
            continue
        # Remove progress bar block characters but keep the rest
        # Progress bars are like: █████▋   38% used
        # Strip leading block chars, keep the percentage
        stripped = re.sub(r"^[\u2580-\u259f\s]+", "", stripped).strip()
        if stripped:
            cleaned.append(stripped)

    if cleaned:
        return UsageInfo(raw_text=pane_text, parsed_lines=cleaned)

    return None
