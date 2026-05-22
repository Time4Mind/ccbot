"""Tests for the ``claude --resume`` "Resume from summary" interactive prompt.

A large/old session resumed via ``claude --resume`` shows a numbered
single-select prompt (resume from summary / full / don't ask again) with a
``❯`` cursor and an ``Enter to confirm · Esc to cancel`` footer — the same
UI class as ExitPlanMode / AskUserQuestion / permission menus. Without
detection ccbot renders no inline keyboard and the resumed session hangs.

These tests assert the detector classifies it as ``ResumeSummary``, surfaces
all three options + the header, and detects the ❯-pointed selected row, so the
prompt flows through the existing generic CB_ASK_* keyboard.
"""

from ccbot.terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
)

# Captured from a real screenshot of ``claude --resume`` on an 11h-old,
# 258.3k-token session.
RESUME_SUMMARY_PANE = (
    "This session is 11h 34m old and 258.3k tokens.\n"
    "\n"
    "Resuming the full session will consume a substantial portion of your "
    "usage limits. We recommend resuming from a summary.\n"
    "\n"
    "❯ 1. Resume from summary (recommended)\n"
    "  2. Resume full session as-is\n"
    "  3. Don't ask me again\n"
    "\n"
    "Enter to confirm · Esc to cancel\n"
)


class TestResumeSummaryDetection:
    def test_classified_as_resume_summary(self):
        result = extract_interactive_content(RESUME_SUMMARY_PANE)
        assert result is not None
        assert result.name == "ResumeSummary"

    def test_is_interactive_ui(self):
        assert is_interactive_ui(RESUME_SUMMARY_PANE) is True

    def test_header_surfaces(self):
        result = extract_interactive_content(RESUME_SUMMARY_PANE)
        assert result is not None
        assert "This session is 11h 34m old" in result.content
        assert "We recommend resuming from a summary" in result.content

    def test_all_three_options_parsed(self):
        result = extract_interactive_content(RESUME_SUMMARY_PANE)
        assert result is not None
        assert "1. Resume from summary (recommended)" in result.content
        assert "2. Resume full session as-is" in result.content
        assert "3. Don't ask me again" in result.content
        # Exactly three numbered options are present.
        numbered = [
            ln
            for ln in result.content.splitlines()
            if ln.strip()[:2] in {"1.", "2.", "3."}
            or ln.strip().startswith(("❯ 1.", "❯ 2.", "❯ 3."))
        ]
        assert len(numbered) == 3

    def test_pointer_marks_selected_row(self):
        # The ❯ cursor sits on option 1 — the selected/highlighted row.
        result = extract_interactive_content(RESUME_SUMMARY_PANE)
        assert result is not None
        pointed = [
            ln for ln in result.content.splitlines() if ln.lstrip().startswith("❯")
        ]
        assert len(pointed) == 1
        assert "1. Resume from summary" in pointed[0]

    def test_footer_surfaces(self):
        result = extract_interactive_content(RESUME_SUMMARY_PANE)
        assert result is not None
        assert "Enter to confirm" in result.content
        assert "Esc to cancel" in result.content

    def test_header_only_anchor_when_options_scrolled(self):
        # Robustness: even if the recommendation paragraph alone leads the
        # capture (header line scrolled off the top), the second top
        # signature anchors detection.
        pane = (
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

    def test_not_confused_with_settings_picker(self):
        # The /model picker shares the ``Enter to confirm`` footer and a
        # ❯-cursor numbered list, but its ``Select model`` header must keep
        # it classified as Settings, not ResumeSummary.
        pane = (
            " Select model\n"
            " Switch between Claude models.\n"
            "\n"
            "   1. Default (recommended)  Opus 4.6\n"
            " ❯ 2. Sonnet                 Sonnet 4.6\n"
            "   3. Haiku                  Haiku 4.5\n"
            "\n"
            " Enter to confirm · Esc to exit\n"
        )
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "Settings"
