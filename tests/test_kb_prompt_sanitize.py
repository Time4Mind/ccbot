"""Regression test: kb-mode prompt sanitization.

Claude Code's AskUserQuestion renders each option's ``preview`` inside
box-drawing frames (``┌ │ ├ ─ …``). Captured verbatim into the kb-mode
card those borders mangled the body, and telegramify auto-collapsed the
long region into an expandable blockquote ("✂ N lines hidden"). The
kb-mode render now strips box-drawing borders and wraps the prompt in a
fenced code block so it renders as literal monospace.
"""

from __future__ import annotations

from ccbot.handlers.card_model import (
    CardState,
    _format_kb_prompt,
    _render_card,
    _sanitize_prompt_block,
)
from ccbot.session_models import Session

# Reconstruction of the reported pane: option previews inside box frames.
BOXED_PROMPT = (
    "□ Механизм\n"
    "Каким механизмом сделать автозапуск ccbot при загрузке телефона?\n"
    "\n"
    "1. Magisk service.d\n"
    "  ┌─────────────────────────────────┐\n"
    "  │ /data/adb/service.d/99-ccbot.sh  │\n"
    "  ├─────────────────────────────────┤\n"
    "2. Termux:Boot\n"
    "3. Оба слоя\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)


def _has_box_drawing(text: str) -> bool:
    return any(0x2500 <= ord(c) <= 0x259F for c in text)


def _sess() -> Session:
    return Session(id="s1", name="t", window_id="@1", workdir="/tmp", state="active")


class TestSanitizePromptBlock:
    def test_strips_all_box_drawing(self):
        out = _sanitize_prompt_block(BOXED_PROMPT)
        assert not _has_box_drawing(out)

    def test_preserves_content(self):
        out = _sanitize_prompt_block(BOXED_PROMPT)
        assert "/data/adb/service.d/99-ccbot.sh" in out
        assert "Magisk service.d" in out
        assert "Termux:Boot" in out
        assert "Оба слоя" in out

    def test_keeps_checkbox_header_glyph(self):
        # □ (U+25A1) is outside the stripped U+2500–U+259F range → kept.
        out = _sanitize_prompt_block(BOXED_PROMPT)
        assert "□ Механизм" in out

    def test_border_only_lines_dropped(self):
        # The ┌──┐ / ├──┤ frame lines must not survive as content.
        out = _sanitize_prompt_block(BOXED_PROMPT)
        for line in out.splitlines():
            assert "┌" not in line and "┐" not in line and "├" not in line

    def test_empty_input(self):
        assert _sanitize_prompt_block("") == ""


class TestKbModeRender:
    def test_prompt_wrapped_in_code_fence(self):
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = BOXED_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        assert "⌨" in out  # waiting-for-input header present
        assert "```" in out  # prompt rendered as a fenced code block

    def test_no_frame_chars_in_rendered_body(self):
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = BOXED_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        # Frame glyphs (only from kb_prompt) are gone. The card's own
        # "─────" separator (U+2500) is allowed and not asserted against.
        assert "┌" not in out and "│" not in out and "├" not in out

    def test_content_survives_render(self):
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = BOXED_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        assert "/data/adb/service.d/99-ccbot.sh" in out

    def test_options_separated_by_divider(self):
        # Even inside the fenced box-frame body, numbered options are split
        # by a literal ───── rule (the fence suppresses MarkdownV2, so the
        # frameless path's markup dividers can't apply). The rule precedes
        # options 2 and 3 only — option 1 keeps its trailing preview line and
        # gets no leading divider → exactly two dividers in the prompt body.
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = BOXED_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        body = out.split("⌨ *Waiting for your input:*", 1)[1]
        assert "─────\n2. Termux:Boot" in body
        assert "─────\n3. Оба слоя" in body
        assert body.count("─────") == 2


# A normal AskUserQuestion with NO box frame — the long-standing working
# case (incl. a benign ── divider). The fix must be a strict no-op here.
NORMAL_PROMPT = (
    "☐ Which approach?\n"
    "Pick a migration strategy:\n"
    "❯ 1. Incremental\n"
    "  2. Big-bang\n"
    "─────\n"
    "  3. Chat about this\n"
    "Enter to select · ↑/↓ to navigate · Esc to cancel\n"
)


class TestNormalPromptUnchanged:
    """The box-frame gate: a prompt without frame glyphs is NOT wrapped
    in a code fence. Its content survives the render verbatim — but
    internal single ``\\n`` are upgraded to CommonMark hard breaks
    (``  \\n``) so numbered options don't collapse into one paragraph
    when the rich parser treats single ``\\n`` as a soft break."""

    def test_no_frame_means_no_code_fence(self):
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = NORMAL_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        # The card chrome (header + the literal "─────" separator) is added
        # by _render_card; the prompt body itself must NOT be code-fenced.
        body = out.split("⌨ *Waiting for your input:*", 1)[1]
        assert "```" not in body
        # Every non-blank pane row survives intact, just on its own line.
        for line in NORMAL_PROMPT.splitlines():
            if line.strip():
                assert line in out

    def test_divider_only_does_not_trip_the_gate(self):
        # A ── divider (U+2500) alone is not a frame → no sanitization.
        assert "❯ 1. Incremental" in NORMAL_PROMPT  # premise
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = NORMAL_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        assert "❯ 1. Incremental" in out  # cursor/options untouched


class TestNumberedOptionsSplit:
    """Numbered options used to collapse into one paragraph because the
    rich parser reads single ``\\n`` as a soft break. The kb-mode body
    now splits each numbered option into its own archive-style block
    separated by a ``─────`` rule — same affordance the /archive list
    uses between sessions."""

    def test_each_option_in_its_own_block(self):
        # Three numbered options + lead-in description → four blocks,
        # joined by the divider so the boundary is visually explicit.
        body = _format_kb_prompt(
            "What do you want to do?\n1. Option A\n2. Option B\n3. Option C"
        )
        assert "What do you want to do?\n\n─────\n\n1. Option A" in body
        assert "1. Option A\n\n─────\n\n2. Option B" in body
        assert "2. Option B\n\n─────\n\n3. Option C" in body

    def test_cursor_marker_still_matches(self):
        # ``❯ 1.`` (and ``> 1.``) — Claude Code marks the active option
        # with a cursor glyph. Must trip the option pattern too.
        body = _format_kb_prompt("Pick:\n❯ 1. Yes\n  2. No")
        assert "Pick:\n\n─────\n\n❯ 1. Yes" in body
        assert "❯ 1. Yes\n\n─────\n\n  2. No" in body

    def test_hint_lines_after_options_get_their_own_block(self):
        # Hints under the option list (model picker case): once the
        # numbered run ends, the trailing hint paragraph is its own
        # block too — separated from the last option by the divider.
        body = _format_kb_prompt(
            "Select model\n"
            "Switch between Claude models.\n"
            "> 1. Default\n"
            "2. Sonnet\n"
            "3. Haiku\n"
            "Enter to confirm · Esc to exit"
        )
        # Hints are joined with hard break (kept as one paragraph)…
        assert "3. Haiku\n\n─────\n\nEnter to confirm · Esc to exit" in body

    def test_source_rule_line_absorbed(self):
        # NORMAL_PROMPT carries its own ``─────`` divider. The generated
        # dividers replace its role — the source rule line must be
        # dropped so the output doesn't end up with consecutive
        # ``─────`` blocks.
        body = _format_kb_prompt(NORMAL_PROMPT)
        # No "─────\n\n─────" double rule anywhere in the body.
        assert "─────\n\n─────" not in body
        # …but the cursor row and the trailing options still made it.
        assert "❯ 1. Incremental" in body
        assert "  2. Big-bang" in body
        assert "  3. Chat about this" in body


class TestPromptWithoutOptions:
    """ExitPlanMode and plain confirmations have no numbered options.
    Those must NOT get the divider treatment — only paragraph + hard-
    break joins, otherwise the prose gets chopped into fake blocks."""

    def test_no_divider_when_no_options(self):
        body = _format_kb_prompt(
            "Ready to start?\nI will modify the schema and re-run migrations."
        )
        # No archive-style separator anywhere.
        assert "─────" not in body
        # Hard-break preserved within the paragraph.
        assert "Ready to start?  \nI will modify the schema" in body

    def test_blank_line_becomes_paragraph_break(self):
        body = _format_kb_prompt("Plan:\n\n- Step 1\n- Step 2\n\nReady?")
        assert "Plan:\n\n- Step 1" in body
        assert "- Step 2\n\nReady?" in body

    def test_empty_prompt(self):
        assert _format_kb_prompt("") == ""


class TestKbModeRenderIntegration:
    def test_outer_parts_separated_by_paragraph_break(self):
        # header / "─────" / "⌨ Waiting…" / prompt are joined with
        # ``\n\n`` so the rich parser doesn't glue them onto one row.
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = "Pick one:\n1. A\n2. B"
        out = _render_card(_sess(), st, user_id=1)
        assert "\n\n─────\n\n⌨ *Waiting for your input:*\n\n" in out

    def test_box_frame_path_no_hard_breaks(self):
        # Code-fenced rendering preserves whitespace natively, so the
        # frameless path's CommonMark hard-break trick (``  \n``) must NOT
        # leak into the fence — it would surface as literal trailing spaces
        # in the monospace block. Option ``─────`` dividers DO apply here
        # (see TestKbModeRender.test_options_separated_by_divider); they're
        # plain ``\n``-joined rule lines, no hard breaks.
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = BOXED_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        body_after_fence = out.split("```", 1)[1]
        body_inside = body_after_fence.split("```", 1)[0]
        assert "  \n" not in body_inside
