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
    # ``exclude`` is a negative guard: if ANY of these patterns matches ANY
    # line in the captured pane, this UIPattern is skipped even when its
    # top/bottom delimiters line up.  Used to keep a deliberately greedy,
    # bottom-less ``❯ N.`` cursor pattern from stealing matches from other
    # numbered-select UIs (Permission / ResumeSummary / Settings) that share
    # the same cursor signature but carry their own header / footer phrases.
    exclude: tuple[re.Pattern[str], ...] = ()


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
        # Tall AskUserQuestion whose ☐ header scrolled off the visible
        # pane. Triggered by the cursor line ``❯ N.`` plus the
        # "Enter to select" footer — both stay visible because they
        # frame the option list. Placed AFTER PermissionPrompt-numbered
        # so a Yes/No prompt still classifies as a permission.
        name="AskUserQuestion",
        top=(re.compile(r"^\s*❯\s*\d+\.\s+\S"),),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
    ),
    UIPattern(
        # Multi-select AskUserQuestion. Options render as numbered
        # bracketed checkboxes (``N. [✔]`` / ``N. [ ]``) and the cursor
        # ``❯`` lives on a SEPARATE ``Submit`` action line — so the moment
        # the user moves the cursor onto Submit, NO line carries the
        # ``❯ N.`` signature the patterns above rely on, and with the ☐
        # header scrolled off the bare-checkbox pattern misses too. That
        # dropped detection mid-prompt: the kb-mode keyboard vanished and
        # the stall-rescue misfired. Anchor on signatures that survive a
        # cursor move: the numbered checkbox option lines (always present)
        # or the ``❯ Submit`` line, framed by the "Enter to select"
        # footer. Placed AFTER PermissionPrompt so a numbered Yes/No wins.
        name="AskUserQuestion",
        top=(
            re.compile(r"^\s*❯\s*Submit\b"),
            re.compile(r"^\s*\d+\.\s*\[[ xX✔✓]\]"),
        ),
        bottom=(re.compile(r"^\s*Enter to select"),),
        min_gap=1,
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
        # ``claude --resume`` on a large/old session offers a numbered
        # single-select: resume from summary / full / don't ask again.
        # Standard ❯-cursor select, so the generic CB_ASK_* keyboard
        # (↑/↓ + Enter/Esc) drives it. Distinguished from other selects
        # by its header ("This session is … old" / "Resuming the full
        # session …") — neither phrase appears in Settings/Permission
        # prompts, so this stays specific. Placed BEFORE Settings: the
        # shared ``Enter to confirm`` bottom is fine because Settings'
        # top (``Select <word>`` / ``Settings:``) never matches here.
        name="ResumeSummary",
        top=(
            re.compile(r"^\s*This session is\b.*\bold\b"),
            re.compile(r"^\s*Resuming the full session"),
        ),
        bottom=(
            re.compile(r"^\s*Enter to confirm"),
            re.compile(r"^\s*Esc to cancel"),
        ),
    ),
    UIPattern(
        name="Settings",
        top=(
            re.compile(r"^\s*Settings:.*tab to cycle"),
            # ``Select <word>`` covers every Claude Code slash picker:
            # /model → "Select model", /effort → "Select reasoning effort",
            # /agents → "Select an agent", /style → "Select output style",
            # etc. The bottom signature (Esc/Enter/filter) keeps this
            # specific to picker modals — false positives in normal
            # output would have to also match one of those terminators.
            re.compile(r"^\s*Select \w"),
        ),
        bottom=(
            re.compile(r"Esc to cancel"),
            re.compile(r"Esc to exit"),
            re.compile(r"Enter to confirm"),
            re.compile(r"^\s*Type to filter"),
        ),
    ),
    UIPattern(
        # A5 hardening — last-resort AskUserQuestion fallback for a TALL,
        # MULTI-QUESTION prompt where BOTH the ☐ header AND the
        # "Enter to select" footer have scrolled off the visible pane,
        # leaving only the ``❯ N.`` cursor line + numbered options. The
        # earlier ``❯ N.`` + "Enter to select" pattern needs the footer;
        # this one drops the bottom anchor entirely (extends to the last
        # non-empty line, mirroring the multi-tab pattern).
        #
        # It is intentionally bottom-less and therefore greedy, so it is
        # placed DEAD LAST: every more-specific numbered-select UI
        # (PermissionPrompt-numbered, ResumeSummary, Settings) precedes it
        # and wins via first-match-wins ordering. The ``exclude`` guard is
        # belt-and-suspenders: if any of those UIs' signature header/footer
        # phrases is still visible (header scrolled but footer didn't, or
        # vice-versa), this pattern bows out so the prompt routes to its
        # correct flow. Only the genuinely ambiguous case — a lone
        # ``❯ N.`` cursor with options and none of those phrases — falls
        # through to AskUserQuestion, which is the safe default for a
        # bare arrow-select with no other signal.
        name="AskUserQuestion",
        top=(
            re.compile(r"^\s*❯\s*\d+\.\s+\S"),
            # Multi-select with the footer ALSO scrolled off — only the
            # checkbox options and/or the ``❯ Submit`` cursor remain.
            re.compile(r"^\s*❯\s*Submit\b"),
            re.compile(r"^\s*\d+\.\s*\[[ xX✔✓]\]"),
        ),
        bottom=(),
        min_gap=1,
        exclude=(
            # PermissionPrompt signatures
            re.compile(r"^\s*❯\s*1\.\s*Yes"),
            re.compile(r"^\s*Do you want to "),
            re.compile(r"^\s*This command requires approval"),
            re.compile(r"^\s*Bash command\s*$"),
            # ResumeSummary signatures
            re.compile(r"^\s*This session is\b.*\bold\b"),
            re.compile(r"^\s*Resuming the full session"),
            # Settings / picker signatures
            re.compile(r"^\s*Settings:.*tab to cycle"),
            re.compile(r"^\s*Select \w"),
            # RestoreCheckpoint / ExitPlanMode signatures
            re.compile(r"^\s*Restore the code"),
            re.compile(r"^\s*Would you like to proceed\?"),
            re.compile(r"^\s*Claude has written up a plan"),
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

    If ``pattern.exclude`` is non-empty and any of its patterns matches any
    line of the capture, the pattern is treated as a non-match (returns
    None) — a negative guard that keeps a greedy bottom-less cursor pattern
    from poaching other numbered-select UIs.
    """
    if pattern.exclude and any(
        e.search(line) for line in lines for e in pattern.exclude
    ):
        return None

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

# Two sets:
#   * SPINNER_ONLY — chars Claude Code uses EXCLUSIVELY for the busy
#     status line (``✻ Thinking…``). Any line starting with one of
#     these is a status line.
#   * SPINNER_AMBIGUOUS — chars that ALSO appear elsewhere (``●`` is
#     used as a bullet in the feedback prompt / Tip line, ``·`` is a
#     general-purpose bullet). For these, parse_status_line additionally
#     requires the line to carry a time-stats parenthetical
#     (``(1m 13s · …``) — that's the distinguishing signature of the
#     real busy status (``● Gallivanting… (53s · ↑2.3k tokens)``).
SPINNER_ONLY = frozenset(["✻", "✽", "✶", "✳", "✢"])
SPINNER_AMBIGUOUS = frozenset(["●", "·"])


_STATUS_TIME_STATS_RE = re.compile(r"\(\s*\d+(?:m\s*\d+)?\s*[smh]")

# Post-thinking finishing markers like ``✻ Cogitated for 2m 23s`` or
# ``✻ Thought for 14s`` use the same spinner glyph as a live status line
# but are *static* — they sit on the pane indefinitely after a turn
# closes. A ``claude --resume`` re-renders the previous state, so these
# lines persist on the pane forever and would otherwise read as
# "permanently busy" to ``parse_status_line``, locking
# ``_wait_for_resume_settle`` until its 200s timeout.
# Discriminator: live status uses present-participle (``Cogitating…``);
# finishing marker uses past-tense ``<verb> for <time>``.
_STATUS_FINISHED_RE = re.compile(
    r"^\S+\s+for\s+\d+(?:\s*m\s*\d+)?\s*[smh]\b",
    re.IGNORECASE,
)


def parse_status_line(pane_text: str) -> str | None:
    """Extract the Claude Code busy-state status line.

    The busy line lives above the input-chrome separator and starts
    with a spinner char (``●``, ``✻``, etc.). Between it and the
    chrome there can be other lines that ALSO start with the same
    char — Claude's tip / feedback prompt::

        … content …
        ● Gallivanting… (1m 13s · ↑2.3k tokens · thought for 8s)   ← STATUS
        ● Tip: Use /btw to ask a quick side question…             ← tip
        ● How is Claude doing this session? (optional)            ← feedback
          1: Bad   2: Fine   3: Good   0: Dismiss
        ────────────────────
        ❯
        ────────────────────
          ⏵⏵ bypass permissions on …

    Discriminator: the status line has a time-stats parenthetical
    like ``(1m 13s ·`` / ``(53s)``. Tips and feedback prompts don't.
    We scan up to 12 lines back from the first chrome separator and
    pick the first spinner line with that signature. If none of the
    spinner lines carry time-stats (older / shorter status formats
    used by the test suite — ``✻ Reading file src/main.py``), fall
    back to the spinner line nearest the chrome.
    """
    if not pane_text:
        return None

    lines = pane_text.split("\n")

    # Anchor on the chrome separator (first ──── line in the tail).
    chrome_idx: int | None = None
    search_start = max(0, len(lines) - 14)
    for i in range(search_start, len(lines)):
        stripped = lines[i].strip()
        if len(stripped) >= 20 and all(c == "─" for c in stripped):
            chrome_idx = i
            break
    if chrome_idx is None:
        return None

    # Scan upward. For SPINNER_ONLY chars (``✻`` etc.) the line is
    # always a status. For SPINNER_AMBIGUOUS chars (``●`` / ``·``) it
    # only counts when the time-stats parenthetical is present.
    upper_bound = max(chrome_idx - 12, -1)
    for i in range(chrome_idx - 1, upper_bound, -1):
        line = lines[i].strip()
        if not line:
            continue
        first = line[0]
        if first in SPINNER_ONLY:
            rest = line[1:].strip()
            # Skip static finishing markers (``Cogitated for 2m 23s``)
            # so they don't read as a permanent busy state — see
            # ``_STATUS_FINISHED_RE`` doc above.
            if _STATUS_FINISHED_RE.match(rest):
                continue
            return rest
        if first in SPINNER_AMBIGUOUS:
            rest = line[1:].strip()
            if _STATUS_TIME_STATS_RE.search(rest):
                return rest
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
