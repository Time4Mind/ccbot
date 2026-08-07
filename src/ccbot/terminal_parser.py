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

from . import terminal_usage as _terminal_usage


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
        # Codex command approval. Enterprise/MDM requirements may force
        # ``approval_policy = unless-trusted`` even when ccbot launches Codex
        # with ``--dangerously-bypass-approvals-and-sandbox``. Codex uses a
        # different header/footer and cursor glyph from Claude, so without a
        # dedicated pattern the command sits in tmux with no Telegram controls.
        name="CodexApproval",
        top=(
            re.compile(r"^\s*Would you like to run the following command\?"),
            re.compile(r"^\s*Would you like to run this command\?"),
        ),
        bottom=(
            re.compile(r"^\s*Press enter to confirm or esc to cancel", re.IGNORECASE),
        ),
    ),
    UIPattern(
        # A tall Codex approval can push its header and the first two choices
        # above the visible tmux viewport.  The negative third choice and the
        # footer remain pinned at the bottom, so use that pair as the fallback
        # signature.  Keep the CodexApproval classification: auto-approve must
        # send Codex's documented ``y`` hotkey even though the visible pane no
        # longer contains the ``1. Yes, proceed (y)`` line.
        name="CodexApproval",
        top=(re.compile(r"^\s*3\.\s*No\b.*\(esc\)\s*$", re.IGNORECASE),),
        bottom=(
            re.compile(r"^\s*Press enter to confirm or esc to cancel", re.IGNORECASE),
        ),
        min_gap=1,
    ),
    UIPattern(
        # Permission menu with numbered choices (no "Esc to cancel" line)
        name="PermissionPrompt",
        top=(re.compile(r"^\s*[❯›>]\s*1\.\s*Yes"),),
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
            # Codex 0.146 reasoning-level picker after /model.
            re.compile(r"Press enter to confirm or esc to go back", re.IGNORECASE),
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


# Compatibility aliases: usage parsing moved to a focused leaf module while
# historical imports from ``ccbot.terminal_parser`` remain valid.
UsageInfo = _terminal_usage.UsageInfo
UsageBreakdown = _terminal_usage.UsageBreakdown
_parse_clock_to_24h = _terminal_usage._parse_clock_to_24h
_parse_pct = _terminal_usage._parse_pct
extract_usage_breakdown = _terminal_usage.extract_usage_breakdown
parse_usage_output = _terminal_usage.parse_usage_output
