"""Tests for the A5 hardening: tall multi-question AskUserQuestion detection.

Claude Code's ``AskUserQuestion`` TUI for a MULTI-question prompt is very
tall — it carries a per-question counter (``1/2``) plus navigation chrome,
and each option can wrap several lines. On a phone-sized tmux pane this
panel overflows the visible viewport. ``tmux capture-pane`` only reads what
is visible, so the capture can lose BOTH framing anchors at once:

  * the ``☐`` / ``✔`` checkbox header (scrolls off the top), AND
  * the ``Enter to select`` footer (scrolls off the bottom),

leaving only the ``❯ N.`` cursor line and the numbered option list. The
three pre-existing AskUserQuestion patterns all need at least one of those
anchors, so the prompt rendered no inline keyboard and was unsteerable from
Telegram.

The 4th AskUserQuestion ``UIPattern`` closes that gap: it anchors on the
bottom-less ``❯ N.`` cursor and extends to the last non-empty line. Because
that cursor signature is shared by PermissionPrompt / ResumeSummary /
Settings pickers, the pattern is placed dead last and carries a negative
``exclude`` guard, so it only catches the genuinely ambiguous lone-cursor
case and never poaches another UI.

These tests assert the footer-and-header-scrolled-off case classifies as
AskUserQuestion, and that the new pattern does not misclassify a Permission
prompt or a ResumeSummary prompt.
"""

from ccbot.terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
)

# A tall, MULTI-question AskUserQuestion (question 1 of 2). On a small pane
# the ``☐`` header AND the ``Enter to select`` footer have BOTH scrolled
# out of the capture — only the ``❯ N.`` cursor + wrapped numbered options
# remain visible.
MULTIQUESTION_FOOTER_AND_HEADER_OFF = (
    "      Which migration strategy should we adopt for the auth service? (1/2)\n"
    "\n"
    "❯ 1. Incremental — migrate the service module-by-module across several\n"
    "       PRs, keeping both code paths alive behind a feature flag until\n"
    "       we reach parity and can delete the old path\n"
    "  2. Big-bang — cut over everything in a single release after a\n"
    "       long-lived branch lands, accepting one riskier merge in exchange\n"
    "       for never running two implementations at once\n"
    "  3. Strangler-fig — stand the new service up alongside the old one and\n"
    "       route traffic across at the edge, retiring routes as they move\n"
    "  4. Let me describe a different approach\n"
)


class TestMultiQuestionFooterAndHeaderScrolledOff:
    def test_classified_as_ask_user(self):
        result = extract_interactive_content(MULTIQUESTION_FOOTER_AND_HEADER_OFF)
        assert result is not None
        assert result.name == "AskUserQuestion"

    def test_is_interactive_ui(self):
        assert is_interactive_ui(MULTIQUESTION_FOOTER_AND_HEADER_OFF) is True

    def test_cursor_and_options_surface(self):
        result = extract_interactive_content(MULTIQUESTION_FOOTER_AND_HEADER_OFF)
        assert result is not None
        # The cursor line and every numbered option must be in the extracted
        # content (it extends from the cursor to the last non-empty line).
        assert "❯ 1. Incremental" in result.content
        assert "2. Big-bang" in result.content
        assert "3. Strangler-fig" in result.content
        assert "4. Let me describe a different approach" in result.content

    def test_no_footer_anchor_present(self):
        # Guards the premise of the test: the footer is genuinely absent, so
        # the older ``❯ N.`` + "Enter to select" pattern could not have fired.
        assert "Enter to select" not in MULTIQUESTION_FOOTER_AND_HEADER_OFF
        # And no checkbox header either.
        assert "☐" not in MULTIQUESTION_FOOTER_AND_HEADER_OFF


class TestNewPatternDoesNotPoachOtherUIs:
    """The bottom-less ``❯ N.`` fallback must never steal a more-specific
    numbered-select UI. The ``exclude`` guard + last-in-order placement keep
    Permission / ResumeSummary classified correctly even when their own
    footer (and one of their framing anchors) is gone."""

    def test_permission_numbered_still_permission(self):
        # The numbered "❯ 1. Yes" permission menu — same cursor signature as
        # the new AskUserQuestion fallback, but PermissionPrompt-numbered
        # precedes it AND the ``❯ 1. Yes`` exclude clause bows the fallback
        # out. Must classify as PermissionPrompt.
        pane = "❯ 1. Yes\n  2. No\n  3. Yes, and don't ask again\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"

    def test_permission_do_you_want_still_permission(self):
        # The textual permission prompt is unaffected — its own pattern wins
        # well before the fallback.
        pane = "  Do you want to proceed?\n  ❯ 1. Yes\n    2. No\n  Esc to cancel\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"

    def test_resume_summary_still_resume_summary(self):
        # Full ResumeSummary prompt — its header + ``Enter to confirm`` footer
        # keep it classified as ResumeSummary, well before the fallback.
        pane = (
            "This session is 11h 34m old and 258.3k tokens.\n"
            "\n"
            "Resuming the full session will consume a substantial portion of "
            "your usage limits. We recommend resuming from a summary.\n"
            "\n"
            "❯ 1. Resume from summary (recommended)\n"
            "  2. Resume full session as-is\n"
            "  3. Don't ask me again\n"
            "\n"
            "Enter to confirm · Esc to cancel\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "ResumeSummary"

    def test_resume_summary_footer_scrolled_off_not_ask_user(self):
        # Even if ResumeSummary's ``Enter to confirm`` footer scrolls off
        # (so its own pattern can no longer match), the ``exclude`` guard
        # sees its header phrases and refuses to relabel it AskUserQuestion.
        # The fallback bows out → result is None (no wrong keyboard), which
        # is the pre-existing behaviour for this degraded capture.
        pane = (
            "This session is 11h 34m old and 258.3k tokens.\n"
            "\n"
            "Resuming the full session will consume a substantial portion of "
            "your usage limits.\n"
            "\n"
            "❯ 1. Resume from summary (recommended)\n"
            "  2. Resume full session as-is\n"
            "  3. Don't ask me again\n"
        )
        result = extract_interactive_content(pane)
        # With both framing anchors gone and the ResumeSummary header still
        # visible, the exclude guard bows the AUQ fallback out → no match.
        assert result is None

    def test_settings_picker_footer_scrolled_off_not_ask_user(self):
        # Same belt-and-suspenders guarantee for the /model picker: its
        # ``Select model`` header keeps the fallback from claiming it.
        pane = (
            " Select model\n"
            "\n"
            "   1. Default (recommended)  Opus 4.6\n"
            " ❯ 2. Sonnet                 Sonnet 4.6\n"
            "   3. Haiku                  Haiku 4.5\n"
        )
        result = extract_interactive_content(pane)
        # The "Select model" header excludes the bottom-less AUQ fallback,
        # and no other pattern matches with the footer gone → no match.
        assert result is None
