"""Tests for Markdown → Telegram MarkdownV2 conversion."""

import pytest

from ccbot.markdown_v2 import _escape_mdv2, convert_markdown
from ccbot.transcript_parser import TranscriptParser

EXP_START = TranscriptParser.EXPANDABLE_QUOTE_START
EXP_END = TranscriptParser.EXPANDABLE_QUOTE_END


class TestEscapeMdv2:
    @pytest.mark.parametrize(
        "input_text,expected",
        [
            (
                "_*[]()~>#+\\-=|{}.!",
                "\\_\\*\\[\\]\\(\\)\\~\\>\\#\\+\\\\\\-\\=\\|\\{\\}\\.\\!",
            ),
            ("hello world 123", "hello world 123"),
            ("", ""),
        ],
        ids=["special-chars", "alphanumeric-unchanged", "empty-string"],
    )
    def test_escape(self, input_text: str, expected: str) -> None:
        assert _escape_mdv2(input_text) == expected


class TestConvertMarkdown:
    def test_plain_text(self) -> None:
        result = convert_markdown("hello world")
        assert "hello world" in result

    def test_bold(self) -> None:
        result = convert_markdown("**bold text**")
        assert "*bold text*" in result
        assert "**bold text**" not in result

    def test_code_block_preserved(self) -> None:
        result = convert_markdown("```python\nprint('hi')\n```")
        assert "```" in result
        assert "print" in result

    def test_expandable_quote_sentinels(self) -> None:
        text = f"{EXP_START}quoted content{EXP_END}"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">quoted content||" in result

    def test_mixed_text_and_expandable_quote(self) -> None:
        text = f"before {EXP_START}inside quote{EXP_END} after"
        result = convert_markdown(text)
        assert EXP_START not in result
        assert EXP_END not in result
        assert ">inside quote||" in result
        assert "before" in result
        assert "after" in result

    def test_html_bold_converted(self) -> None:
        # The bug: claude sometimes emits <b>X</b> instead of **X**
        # because the user's global instructions point it at HTML
        # parse_mode. The bot is on MarkdownV2 — pre-convert so the
        # literal tag doesn't land in chat.
        result = convert_markdown("<b>Forrest Fenn</b> — $2M")
        assert "<b>" not in result
        assert "</b>" not in result
        assert "Forrest Fenn" in result

    def test_html_inline_tags_outside_fences(self) -> None:
        result = convert_markdown("<i>italic</i> and <code>snip</code>")
        assert "<i>" not in result and "</i>" not in result
        assert "<code>" not in result and "</code>" not in result
        assert "italic" in result
        assert "snip" in result

    def test_html_tags_inside_code_fence_preserved(self) -> None:
        # Discussing HTML in a fenced block — pre-conversion must not
        # rewrite the tags to ``**``/backticks. The downstream MarkdownV2
        # escape will turn ``<b>`` into ``<b\>`` but that renders back
        # as ``<b>`` inside the fence — the literal characters are what
        # we care about.
        text = "```html\n<b>example</b>\n```"
        result = convert_markdown(text)
        # Tag opener + name + closer must survive (escaped or not).
        assert "<b" in result and "example" in result and "/b" in result
        # And no Markdown bold conversion should have leaked in.
        assert "**example**" not in result
