"""Tests for multi-select AskUserQuestion detection (cursor on ``Submit``).

Claude Code's MULTI-select ``AskUserQuestion`` renders each option as a
numbered, bracketed checkbox (``N. [✔]`` / ``N. [ ]``) and parks the cursor
``❯`` on a SEPARATE ``Submit`` action line. The earlier AskUserQuestion
patterns all key on either a bare ``☐`` header glyph or a ``❯ N.`` cursor on
a numbered option — neither of which is present once the user moves the
cursor onto ``Submit`` and the ``☐`` header has scrolled off the pane. The
prompt then went undetected for that frame: the kb-mode keyboard vanished
and the stall-rescue could misfire.

The new pattern anchors on signatures that survive a cursor move — the
numbered checkbox option lines (always present) or the ``❯ Submit`` line —
framed by the ``Enter to select`` footer, with a bottom-less fallback for
the footer-also-scrolled-off case.
"""

from ccbot.terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
)

# Cursor parked on ``❯ Submit``; the ``☐`` header has scrolled off but the
# footer is still visible (the reported repro).
MULTISELECT_CURSOR_ON_SUBMIT = (
    "  (включай только то, чем не пользуешься)\n"
    "\n"
    "  1. [✔] Игровое (Game Space + COSA + GameCenter)\n"
    "  com.oplus.games (43 сервиса), com.oplus.cosa (41).\n"
    "  2. [✔] Quick Apps (мгновенные приложения)\n"
    "  com.nearme.instant.platform (10 сервисов).\n"
    "  3. [✔] Shelf / экран -1\n"
    "  4. [✔] Breeno-голос + App Market\n"
    "  5. [ ] Type something\n"
    " ❯ Submit\n"
    "─────\n"
    "  6. Chat about this\n"
    "  Enter to select · ↑/↓ to navigate · ctrl+g to edit in VS Code · Esc to cancel\n"
)

# Same prompt, but the footer ALSO scrolled off — only checkbox options and
# the ``❯ Submit`` cursor remain.
MULTISELECT_SUBMIT_FOOTER_OFF = (
    "  3. [✔] Shelf / экран -1\n"
    "  4. [✔] Breeno-голос + App Market\n"
    "  5. [ ] Type something\n"
    " ❯ Submit\n"
)

# Cursor on a numbered checkbox option (before the user moved to Submit).
MULTISELECT_CURSOR_ON_OPTION = (
    "  1. [✔] Игровое\n"
    " ❯ 2. [✔] Quick Apps\n"
    "  5. [ ] Type something\n"
    "  Submit\n"
    "  Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)


class TestMultiSelectCursorOnSubmit:
    def test_classified_as_ask_user(self):
        result = extract_interactive_content(MULTISELECT_CURSOR_ON_SUBMIT)
        assert result is not None
        assert result.name == "AskUserQuestion"

    def test_is_interactive_ui(self):
        assert is_interactive_ui(MULTISELECT_CURSOR_ON_SUBMIT) is True

    def test_submit_and_options_surface(self):
        result = extract_interactive_content(MULTISELECT_CURSOR_ON_SUBMIT)
        assert result is not None
        assert "❯ Submit" in result.content
        assert "1. [✔]" in result.content

    def test_premise_no_old_anchor(self):
        # The old AUQ patterns could not have fired: no bare ☐ header glyph,
        # and the ``❯`` cursor is on Submit, not on a ``❯ N.`` option.
        assert "☐" not in MULTISELECT_CURSOR_ON_SUBMIT
        assert "❯ 1." not in MULTISELECT_CURSOR_ON_SUBMIT
        assert "❯ Submit" in MULTISELECT_CURSOR_ON_SUBMIT


class TestMultiSelectFooterScrolledOff:
    def test_classified_as_ask_user(self):
        result = extract_interactive_content(MULTISELECT_SUBMIT_FOOTER_OFF)
        assert result is not None
        assert result.name == "AskUserQuestion"

    def test_premise_no_footer(self):
        assert "Enter to select" not in MULTISELECT_SUBMIT_FOOTER_OFF


class TestMultiSelectCursorOnOption:
    def test_classified_as_ask_user(self):
        # Cursor still on a numbered option → already detectable; assert the
        # new checkbox-aware patterns don't regress this case.
        result = extract_interactive_content(MULTISELECT_CURSOR_ON_OPTION)
        assert result is not None
        assert result.name == "AskUserQuestion"


class TestNewPatternDoesNotPoachOtherUIs:
    def test_permission_numbered_still_permission(self):
        pane = "❯ 1. Yes\n  2. No\n  3. Yes, and don't ask again\n"
        result = extract_interactive_content(pane)
        assert result is not None
        assert result.name == "PermissionPrompt"

    def test_settings_picker_not_ask_user(self):
        # /model picker uses unbracketed numbered options — the checkbox
        # anchor must not claim it, and its "Select model" header excludes
        # the bottom-less fallback.
        pane = (
            " Select model\n"
            "\n"
            "   1. Default (recommended)  Opus 4.6\n"
            " ❯ 2. Sonnet                 Sonnet 4.6\n"
            "   3. Haiku                  Haiku 4.5\n"
        )
        result = extract_interactive_content(pane)
        assert result is None or result.name != "AskUserQuestion"

    def test_normal_prose_not_interactive(self):
        pane = "Here is a list:\n  1. first thing\n  2. second thing\nDone.\n"
        assert is_interactive_ui(pane) is False
