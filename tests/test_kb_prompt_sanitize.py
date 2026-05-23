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
    """The box-frame gate: a prompt without frame glyphs renders exactly as
    before — no sanitization, no code fence. Guards the working case."""

    def test_no_frame_means_no_code_fence(self):
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = NORMAL_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        # The card chrome (header + the literal "─────" separator) is added
        # by _render_card; the prompt body itself must NOT be code-fenced.
        body = out.split("⌨ *Waiting for your input:*\n", 1)[1]
        assert "```" not in body
        # ...and the prompt is carried verbatim (incl. its own divider).
        assert NORMAL_PROMPT in out

    def test_divider_only_does_not_trip_the_gate(self):
        # A ── divider (U+2500) alone is not a frame → no sanitization.
        assert "❯ 1. Incremental" in NORMAL_PROMPT  # premise
        st = CardState()
        st.in_kb_mode = True
        st.kb_prompt = NORMAL_PROMPT
        out = _render_card(_sess(), st, user_id=1)
        assert "❯ 1. Incremental" in out  # cursor/options untouched
