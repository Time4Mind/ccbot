"""Tests for ``_split_tool_text`` — parses transcript_format's tool text into
(name, args, summary, content) so the head shows only the tool name while
args / output land under the spoiler."""

from __future__ import annotations

from ccbot.handlers.notifications import _split_tool_text


class TestSplitToolText:
    def test_empty(self) -> None:
        assert _split_tool_text("") == ("", "", "", "")

    def test_basic_name_and_args(self) -> None:
        name, args, summary, content = _split_tool_text("**Bash**(npm test)")
        assert name == "Bash"
        assert args == "npm test"
        assert summary == ""
        assert content == ""

    def test_read_with_path(self) -> None:
        name, args, _, _ = _split_tool_text("**Read**(/root/ccbot/src/main.py)")
        assert name == "Read"
        assert args == "/root/ccbot/src/main.py"

    def test_long_bash_args_pulled_out(self) -> None:
        cmd = "uv run python /tmp/render_check.py 2>&1 | tail -40"
        name, args, _, _ = _split_tool_text(f"**Bash**({cmd})")
        assert name == "Bash"
        assert args == cmd

    def test_summary_extracted_from_corner(self) -> None:
        raw = "**Bash**(ls)\n  ⎿  Output 5 lines"
        name, args, summary, content = _split_tool_text(raw)
        assert name == "Bash"
        assert args == "ls"
        assert summary == "Output 5 lines"
        assert content == ""

    def test_expquote_content_extracted(self) -> None:
        body = "line 1\nline 2\nline 3"
        raw = (
            "**Bash**(ls)\n"
            "  ⎿  Output 3 lines\n"
            f"\x02EXPQUOTE_START\x02{body}\x02EXPQUOTE_END\x02"
        )
        _, _, _, content = _split_tool_text(raw)
        assert content == body

    def test_duplicate_head_in_content_stripped(self) -> None:
        # transcript_parser sometimes re-embeds the head line at the top
        # of the EXPQUOTE body — make sure we don't show it twice.
        raw = (
            "**Bash**(ls)\n"
            "  ⎿  Output 2 lines\n"
            "\x02EXPQUOTE_START\x02"
            "**Bash**(ls)\n  ⎿  Output 2 lines\nfile1.txt\nfile2.txt"
            "\x02EXPQUOTE_END\x02"
        )
        _, _, _, content = _split_tool_text(raw)
        assert content == "file1.txt\nfile2.txt"

    def test_orphan_head_falls_back_to_full_name(self) -> None:
        # tool_result fallback path: text might not look like ``Name(...)``.
        name, args, _, _ = _split_tool_text("PASS 24 tests")
        assert "PASS" in name
        assert args == ""
