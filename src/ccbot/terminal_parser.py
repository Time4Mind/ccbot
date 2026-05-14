"""Terminal output parser — detects Claude Code UI elements in pane text.

Parses captured tmux pane content to detect:
  - Interactive UIs (AskUserQuestion, ExitPlanMode, Permission Prompt,
    RestoreCheckpoint) via regex-based UIPattern matching with top/bottom
    delimiters.
  - Status line (spinner characters + working text) by scanning from bottom up.

All Claude Code text patterns live here. To support a new UI type or
a changed Claude Code version, edit UI_PATTERNS / STATUS_SPINNERS.

Key functions: is_interactive_ui(), extract_interactive_content(),
parse_status_line(), strip_pane_chrome(), extract_bash_output().
"""

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str  # The extracted display content
    name: str = ""  # Pattern name that matched (e.g. "AskUserQuestion")


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region.

    Extraction scans lines top-down: the first line matching any `top` pattern
    marks the start, the first subsequent line matching any `bottom` pattern
    marks the end.  Both boundary lines are included in the extracted content.

    ``top`` and ``bottom`` are tuples of compiled regexes — any single match
    is sufficient.  This accommodates wording changes across Claude Code
    versions (e.g. a reworded confirmation prompt).
    """

    name: str  # Descriptive label (not used programmatically)
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 2  # minimum lines between top and bottom (inclusive)


# ── UI pattern definitions (order matters — first match wins) ────────────

UI_PATTERNS: list[UIPattern] = [
    UIPattern(
        name="ExitPlanMode",
        top=(
            re.compile(r"^\s*Would you like to proceed\?"),
            # v2.1.29+: longer prefix that may wrap across lines
            re.compile(r"^\s*Claude has written up a plan"),
        ),
        bottom=(
            re.compile(r"^\s*ctrl-g to edit in "),
            re.compile(r"^\s*Esc to (cancel|exit)"),
        ),
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*←\s+[☐✔☒]"),),  # Multi-tab: no bottom needed
        bottom=(),
        min_gap=1,
    ),
    UIPattern(
        name="AskUserQuestion",
        top=(re.compile(r"^\s*[☐✔☒]"),),  # Single-tab: bottom required
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        name="PermissionPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*Do you want to create \S"),
            re.compile(r"^\s*Do you want to delete \S"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*❯\s*1\.\s*Yes"),),
        bottom=(),
        min_gap=2,
    ),
    UIPattern(
        # Bash command approval
        name="BashApproval",
        top=(
            re.compile(r"^\s*Bash command\s*$"),
            re.compile(r"^\s*This command requires approval"),
        ),
        bottom=(re.compile(r"^\s*Esc to cancel"),),
    ),
    UIPattern(
        name="RestoreCheckpoint",
        top=(re.compile(r"^\s*Restore the code"),),
        bottom=(re.compile(r"^\s*Enter to continue"),),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select model"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
]


# ── Post-processing ──────────────────────────────────────────────────────

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace lines of 5+ ─ characters with exactly ─────."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


# ── Core extraction ──────────────────────────────────────────────────────


def _try_extract(lines: list[str], pattern: UIPattern) -> InteractiveUIContent | None:
    """Try to extract content matching a single UI pattern.

    When ``pattern.bottom`` is empty, the region extends from the top marker
    to the last non-empty line (used for multi-tab AskUserQuestion where the
    bottom delimiter varies by tab).
    """
    top_idx: int | None = None
    bottom_idx: int | None = None

    for i, line in enumerate(lines):
        if top_idx is None:
            if any(p.search(line) for p in pattern.top):
                top_idx = i
        elif pattern.bottom and any(p.search(line) for p in pattern.bottom):
            bottom_idx = i
            break

    if top_idx is None:
        return None

    # No bottom patterns → use last non-empty line as boundary
    if not pattern.bottom:
        for i in range(len(lines) - 1, top_idx, -1):
            if lines[i].strip():
                bottom_idx = i
                break

    if bottom_idx is None or bottom_idx - top_idx < pattern.min_gap:
        return None

    content = "\n".join(lines[top_idx : bottom_idx + 1]).rstrip()
    return InteractiveUIContent(content=_shorten_separators(content), name=pattern.name)


# ── Public API ───────────────────────────────────────────────────────────


def extract_interactive_content(pane_text: str) -> InteractiveUIContent | None:
    """Extract content from an interactive UI in terminal output.

    Tries each UI pattern in declaration order; first match wins.
    Returns None if no recognizable interactive UI is found.
    """
    if not pane_text:
        return None

    lines = pane_text.strip().split("\n")
    for pattern in UI_PATTERNS:
        result = _try_extract(lines, pattern)
        if result:
            return result
    return None


def is_interactive_ui(pane_text: str) -> bool:
    """Check if terminal currently shows an interactive UI."""
    return extract_interactive_content(pane_text) is not None


# ── Status line parsing ─────────────────────────────────────────────────

# Spinner characters Claude Code uses in its status line
STATUS_SPINNERS = frozenset(["·", "✻", "✽", "✶", "✳", "✢"])


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code status line from terminal output.

    The status line (spinner + working text) appears immediately above
    the chrome separator (a full line of ``─`` characters).  We locate
    the separator first, then check the line just above it — this avoids
    false positives from ``·`` bullets in Claude's regular output.

    Returns the text after the spinner, or None if no status line found.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Find the chrome separator: topmost ──── line in the last 10 lines
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break

    if chrome_idx is None:
        return None  # No chrome visible — can't determine status

    # Check lines just above the separator (skip blanks, up to 4 lines)
    for i in range(chrome_idx - 1, max(chrome_idx - 5, -1), -1):
        line = lines[i].strip()
        if not line:
            continue
        if line[0] in STATUS_SPINNERS:
            return line[1:].strip()
        # First non-empty line above separator isn't a spinner → no status
        return None
    return None


# ── Pane chrome stripping & bash output extraction ─────────────────────


def strip_pane_chrome(lines: list[str]) -> list[str]:
    """Strip Claude Code's bottom chrome (prompt area + status bar).

    The bottom of the pane looks like::

        ────────────────────────  (separator)
        ❯                        (prompt)
        ────────────────────────  (separator)
          [Opus 4.6] Context: 34%
          ⏵⏵ bypass permissions…

    This function finds the topmost ``────`` separator in the last 10 lines
    and strips everything from there down.
    """
    search_start = max(0, len(lines) - 10)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            return lines[:i]
    return lines


def extract_bash_output(pane_text: str, command: str) -> str | None:
    """Extract ``!`` command output from a captured tmux pane.

    Searches from the bottom for the ``! <command>`` echo line, then
    returns that line and everything below it (including the ``⎿`` output).
    Returns *None* if the command echo wasn't found.
    """
    lines = strip_pane_chrome(pane_text.splitlines())

    # Find the last "! <command>" echo line (search from bottom).
    # Match on the first 10 chars of the command in case the line is truncated.
    cmd_idx: int | None = None
    match_prefix = command[:10]
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith(f"! {match_prefix}") or stripped.startswith(
            f"!{match_prefix}"
        ):
            cmd_idx = i
            break

    if cmd_idx is None:
        return None

    # Include the command echo line and everything after it
    raw_output = lines[cmd_idx:]

    # Strip trailing empty lines
    while raw_output and not raw_output[-1].strip():
        raw_output.pop()

    if not raw_output:
        return None

    return "\n".join(raw_output).strip()


# ── Usage modal parsing ──────────────────────────────────────────────────────────


@dataclass
class UsageInfo:
    """Parsed output from Claude Code's /usage modal."""

    raw_text: str  # Full captured pane text
    parsed_lines: list[str]  # Cleaned content lines from the modal


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
