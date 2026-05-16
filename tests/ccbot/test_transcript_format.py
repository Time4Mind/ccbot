"""Tests for transcript_format helpers (extracted from transcript_parser)."""

from ccbot import transcript_format


class TestFormatToolUseSummary:
    def test_read_uses_file_path(self) -> None:
        assert (
            transcript_format.format_tool_use_summary("Read", {"file_path": "/x.py"})
            == "**Read**(/x.py)"
        )

    def test_bash_uses_command(self) -> None:
        assert (
            transcript_format.format_tool_use_summary("Bash", {"command": "ls"})
            == "**Bash**(ls)"
        )

    def test_no_dict_returns_bare_name(self) -> None:
        assert transcript_format.format_tool_use_summary("X", "junk") == "**X**"

    def test_empty_dict_returns_bare_name(self) -> None:
        assert transcript_format.format_tool_use_summary("Foo", {}) == "**Foo**"

    def test_truncates_long_summary(self) -> None:
        long = "a" * 500
        result = transcript_format.format_tool_use_summary("Read", {"file_path": long})
        assert result.endswith("…)")
        assert len(result) < len(long)

    def test_todowrite_counts_items(self) -> None:
        assert (
            transcript_format.format_tool_use_summary("TodoWrite", {"todos": [1, 2, 3]})
            == "**TodoWrite**(3 item(s))"
        )


class TestExtractToolResultText:
    def test_string_passthrough(self) -> None:
        assert transcript_format.extract_tool_result_text("hello") == "hello"

    def test_list_of_text_blocks(self) -> None:
        content = [
            {"type": "text", "text": "a"},
            {"type": "text", "text": "b"},
        ]
        assert transcript_format.extract_tool_result_text(content) == "a\nb"

    def test_skips_non_text_blocks(self) -> None:
        content = [
            {"type": "text", "text": "a"},
            {"type": "image", "source": {}},
            {"type": "text", "text": "b"},
        ]
        assert transcript_format.extract_tool_result_text(content) == "a\nb"

    def test_strips_tool_use_error_envelope_string(self) -> None:
        # Claude Code wraps hook-rejected results in
        # ``<tool_use_error>...</tool_use_error>``. Without stripping,
        # the literal tags ended up in card heads like
        # "Bash · Error: <tool_use_error>Blocked: sleep 25 ...".
        wrapped = (
            "<tool_use_error>Blocked: sleep 25 followed by: cat /tmp/x. "
            "To wait for a condition, use ...</tool_use_error>"
        )
        result = transcript_format.extract_tool_result_text(wrapped)
        assert "<tool_use_error>" not in result
        assert "</tool_use_error>" not in result
        assert result.startswith("Blocked: sleep 25")

    def test_strips_tool_use_error_envelope_in_list(self) -> None:
        content = [
            {
                "type": "text",
                "text": ("<tool_use_error>Blocked: rm -rf /</tool_use_error>"),
            }
        ]
        result = transcript_format.extract_tool_result_text(content)
        assert "<tool_use_error>" not in result
        assert result == "Blocked: rm -rf /"

    def test_no_envelope_is_passthrough(self) -> None:
        # Make sure the strip path doesn't touch normal output.
        assert (
            transcript_format.extract_tool_result_text("normal output")
            == "normal output"
        )


class TestExtractToolResultImages:
    def test_no_images_returns_none(self) -> None:
        assert (
            transcript_format.extract_tool_result_images(
                [{"type": "text", "text": "x"}]
            )
            is None
        )

    def test_decodes_base64_image(self) -> None:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": "aGVsbG8=",  # "hello"
                },
            }
        ]
        result = transcript_format.extract_tool_result_images(content)
        assert result is not None
        assert result[0] == ("image/png", b"hello")


class TestFormatEditDiff:
    def test_simple_diff(self) -> None:
        out = transcript_format.format_edit_diff("a\nb\nc\n", "a\nB\nc\n")
        # Header lines stripped; we just expect changes marked.
        assert any(line.startswith("-b") for line in out.split("\n"))
        assert any(line.startswith("+B") for line in out.split("\n"))


class TestFormatExpandableQuote:
    def test_wraps_with_sentinels(self) -> None:
        out = transcript_format.format_expandable_quote("hi")
        assert out.startswith(transcript_format.EXPANDABLE_QUOTE_START)
        assert out.endswith(transcript_format.EXPANDABLE_QUOTE_END)
        assert "hi" in out


class TestFormatToolResultText:
    def test_empty_returns_empty(self) -> None:
        assert transcript_format.format_tool_result_text("") == ""

    def test_read_returns_line_count(self) -> None:
        assert (
            transcript_format.format_tool_result_text("a\nb\nc", "Read")
            == "  ⎿  Read 3 lines"
        )

    def test_grep_returns_match_count(self) -> None:
        out = transcript_format.format_tool_result_text("hit1\nhit2", "Grep")
        assert "Found 2 matches" in out

    def test_bash_includes_expandable_quote(self) -> None:
        out = transcript_format.format_tool_result_text("output", "Bash")
        assert transcript_format.EXPANDABLE_QUOTE_START in out
