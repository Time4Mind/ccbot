"""Tests for ``_strip_for_card`` — strips EXPQUOTE residue + home path.

The card now renders with parse_mode=MarkdownV2 so we DO NOT strip
markdown markers (``**bold**``, ``_italic_``, backticks) — the
MarkdownV2 converter renders them. Only EXPQUOTE sentinels (which
have no business inside a one-line head) and ``$HOME`` → ``~`` are
collapsed by this helper.
"""

from __future__ import annotations

import os

from ccbot.handlers.notifications import _extract_expquote_inner, _strip_for_card


class TestStripForCard:
    def test_empty(self) -> None:
        assert _strip_for_card("") == ""

    def test_passthrough_plain_text(self) -> None:
        assert _strip_for_card("hello") == "hello"

    def test_passes_markdown_markers_through(self) -> None:
        # Markdown markers are intentionally KEPT — convert_markdown
        # turns them into MarkdownV2 bold/italic/code.
        assert _strip_for_card("**bold**") == "**bold**"
        assert _strip_for_card("_italic_") == "_italic_"
        assert _strip_for_card("`code`") == "`code`"

    def test_strips_expquote_block(self) -> None:
        body = "\x02EXPQUOTE_START\x02inner content\x02EXPQUOTE_END\x02"
        text = f"prefix {body} suffix"
        out = _strip_for_card(text)
        assert "EXPQUOTE" not in out
        assert out == "prefix  suffix"

    def test_strips_orphan_sentinels(self) -> None:
        # Lone start or end sentinel (e.g. nested quote that got
        # half-extracted in upstream parsing) is also nuked.
        out = _strip_for_card("hello \x02EXPQUOTE_START\x02 world")
        assert "EXPQUOTE" not in out
        out = _strip_for_card("hello \x02EXPQUOTE_END\x02 world")
        assert "EXPQUOTE" not in out

    def test_home_path_collapses(self) -> None:
        home = os.path.expanduser("~")
        if not home or home == "/":
            return  # skip on weird envs
        out = _strip_for_card(f"file at {home}/proj/main.py")
        assert "~" in out
        assert home not in out


class TestExtractExpquoteInner:
    def test_no_block_returns_empty(self) -> None:
        assert _extract_expquote_inner("plain text") == ""

    def test_returns_first_block_content(self) -> None:
        text = "\x02EXPQUOTE_START\x02hello\x02EXPQUOTE_END\x02"
        assert _extract_expquote_inner(text) == "hello"

    def test_returns_only_first_block(self) -> None:
        text = (
            "\x02EXPQUOTE_START\x02first\x02EXPQUOTE_END\x02"
            " sep "
            "\x02EXPQUOTE_START\x02second\x02EXPQUOTE_END\x02"
        )
        assert _extract_expquote_inner(text) == "first"
