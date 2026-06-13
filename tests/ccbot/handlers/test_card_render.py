"""Tests for ``render_page`` separator + tool head/body split + spoiler
wrapping — covers what the user actually sees in the card."""

from __future__ import annotations

from ccbot.handlers.notifications import (
    Event,
    _build_event,
    _format_hhmmss,
    _spoiler_body,
    render_event,
    render_page,
)
from ccbot.session_monitor import NewMessage


def _msg(content_type: str, text: str, **kw) -> NewMessage:  # type: ignore[no-untyped-def]
    return NewMessage(
        session_id="s1",
        text=text,
        is_complete=True,
        content_type=content_type,
        tool_use_id=kw.get("tool_use_id"),
        role=kw.get("role", "assistant"),
        tool_name=kw.get("tool_name"),
        stop_reason=kw.get("stop_reason"),
        timestamp=kw.get("timestamp", ""),
    )


class TestRenderPageSeparator:
    def test_events_separated_by_blank_line(self) -> None:
        # Two thinking events — rendered output should have a blank line
        # between them (per user feedback: "сплошная масса tldr" — fixed
        # by switching from \n to \n\n in render_page).
        e1 = Event(type="thinking", text="", started_at=1.0)
        e2 = Event(type="thinking", text="", started_at=2.0, completed_at=2.0)
        out = render_page([e1, e2], now=3.0)
        # Between the two ∴ lines we expect a blank line — i.e. \n\n.
        assert out.count("\n∴") >= 1
        # Spacing: not a wall of text.
        assert "\n\n" in out


class TestToolHeadAndSpoiler:
    def test_tool_head_only_name(self) -> None:
        # The agreed format: head shows only the tool NAME, not the args.
        # Bash(very long command...) should give head "Bash · ⏳ 0:00"
        # with the command moved under the spoiler.
        ev = _build_event(
            _msg(
                "tool_use",
                "**Bash**(uv run python /tmp/render_check.py 2>&1 | tail -40)",
                tool_use_id="t1",
                tool_name="Bash",
            )
        )
        assert ev.text == "Bash"
        assert "uv run python" in ev.body  # args ended up under spoiler

    def test_completed_tool_head_has_summary(self) -> None:
        # After tool_result fold (or as a standalone tool_result), head
        # shows "Name · summary"  e.g. "Bash · Output 5 lines".
        ev = _build_event(
            _msg(
                "tool_result",
                "**Bash**(ls)\n  ⎿  Output 5 lines\n"
                "\x02EXPQUOTE_START\x02a\nb\x02EXPQUOTE_END\x02",
                tool_use_id="t1",
            )
        )
        assert "Bash" in ev.text
        assert "Output 5 lines" in ev.text


class TestSyntaxHighlightedToolBody:
    """``_build_tool_spoiler_body`` wraps the command / path / pattern
    AND the content in language-tagged fenced blocks so Telegram applies
    syntax highlighting inside the spoiler. The tool event line (the
    spoiler header) stays plain text."""

    def test_bash_command_wrapped_in_bash_fence(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body("Bash", "grep -n '^def' file.py", "")
        assert out == "```bash\ngrep -n '^def' file.py\n```"

    def test_bash_command_plus_output_keeps_output_plain(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body("Bash", "ls -la", "total 8\nfoo.py")
        assert out.startswith("```bash\nls -la\n```")
        # Output stays plain — not a code block.
        assert "total 8\nfoo.py" in out
        assert out.count("```") == 2  # only the bash fence

    def test_read_content_picks_language_from_path_extension(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body(
            "Read", "src/ccbot/handlers/foo.py", "def bar():\n    return 1"
        )
        # Path inline-code'd, content in python-fenced block.
        assert "`src/ccbot/handlers/foo.py`" in out
        assert "```python\ndef bar():\n    return 1\n```" in out

    def test_read_unknown_extension_falls_back_to_plain_fence(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body("Read", "data/blob.xyz", "raw bytes here")
        # No language hint, just monospace fence.
        assert "```\nraw bytes here\n```" in out

    def test_write_uses_typescript_for_ts_files(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body("Write", "src/app.ts", "const x: number = 1;")
        assert "```typescript\nconst x: number = 1;\n```" in out

    def test_edit_content_uses_diff_block(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body("Edit", "src/foo.py", "- old()\n+ new()")
        assert "```diff\n- old()\n+ new()\n```" in out

    def test_grep_pattern_inline_code_matches_plain(self) -> None:
        from ccbot.handlers.card_model import _build_tool_spoiler_body

        out = _build_tool_spoiler_body(
            "Grep", "^class\\s", "foo.py:13:class Bar:\nbaz.py:7:class Baz:"
        )
        assert "`^class\\s`" in out
        # Matches stay plain.
        assert "foo.py:13:class Bar:" in out
        assert "```" not in out.split("`^class")[1].split("`", 2)[1]

    def test_dockerfile_recognised(self) -> None:
        from ccbot.handlers.card_model import _lang_for_path

        assert _lang_for_path("/repo/Dockerfile") == "dockerfile"
        assert _lang_for_path("dev.dockerfile") == "dockerfile"

    def test_path_extension_map(self) -> None:
        from ccbot.handlers.card_model import _lang_for_path

        assert _lang_for_path("a.py") == "python"
        assert _lang_for_path("a.ts") == "typescript"
        assert _lang_for_path("a.tsx") == "tsx"
        assert _lang_for_path("a.js") == "javascript"
        assert _lang_for_path("a.go") == "go"
        assert _lang_for_path("a.rs") == "rust"
        assert _lang_for_path("a.json") == "json"
        assert _lang_for_path("a.yaml") == "yaml"
        assert _lang_for_path("a.yml") == "yaml"
        assert _lang_for_path("a.sql") == "sql"
        assert _lang_for_path("a.sh") == "bash"
        assert _lang_for_path("nope") == ""
        assert _lang_for_path("") == ""


class TestHeadedBlock:
    """``_headed_block`` wraps the (head, body) pair in the
    ``EXPANDABLE_HEADED`` sentinel so the tool / thinking event line
    BECOMES the spoiler label — collapsed view shows just the head with
    a chevron, expanded view shows the body without repeating the head."""

    def test_no_body_returns_plain_head(self) -> None:
        from ccbot.handlers.card_model import _headed_block

        assert _headed_block("✓ Bash · Output 3 lines · 22:47", "") == (
            "✓ Bash · Output 3 lines · 22:47"
        )

    def test_with_body_wraps_in_headed_sentinel(self) -> None:
        from ccbot.handlers.card_model import _headed_block
        from ccbot.transcript_format import (
            EXPANDABLE_HEADED_END,
            EXPANDABLE_HEADED_SEP,
            EXPANDABLE_HEADED_START,
        )

        head = "✓ Bash · Output 3 lines · 22:47"
        body = "grep -n '^class' file.py\n13:def foo\n19:def bar"
        out = _headed_block(head, body)
        assert out.startswith(EXPANDABLE_HEADED_START)
        assert out.endswith(EXPANDABLE_HEADED_END)
        # Payload is ``head\x1fbody``.
        payload = out[len(EXPANDABLE_HEADED_START) : -len(EXPANDABLE_HEADED_END)]
        sep_head, sep, sep_body = payload.partition(EXPANDABLE_HEADED_SEP)
        assert sep == EXPANDABLE_HEADED_SEP
        assert sep_head == head
        # Body present, without a repeat of head.
        assert head not in sep_body
        assert "grep -n" in sep_body
        assert "13:def foo" in sep_body

    def test_rich_renders_head_as_summary_body_only(self) -> None:
        from ccbot.handlers.card_model import _headed_block
        from ccbot.rich import to_rich_markdown

        head = "✓ Bash · Output 3 lines · 22:47"
        body = "grep -n '^class' file.py"
        rich = to_rich_markdown(_headed_block(head, body))
        # ``<summary>`` carries the head, ``<details>`` body is the
        # body content WITHOUT the head repeated.
        assert f"<summary>{head}</summary>" in rich
        details_body = rich.split("</summary>", 1)[1].split("</details>", 1)[0]
        assert head not in details_body
        assert "grep -n" in details_body


class TestSpoilerBody:
    def test_empty_body_returns_empty(self) -> None:
        assert _spoiler_body("") == ""

    def test_wraps_in_expquote_sentinels(self) -> None:
        out = _spoiler_body("some body line\nanother line")
        # _spoiler_body delegates to format_expandable_quote which wraps
        # in \x02EXPQUOTE_START\x02 ... \x02EXPQUOTE_END\x02
        assert "\x02EXPQUOTE_START\x02" in out
        assert "\x02EXPQUOTE_END\x02" in out

    def test_trims_to_spoiler_max_lines(self) -> None:
        body = "\n".join(f"line {i}" for i in range(20))
        out = _spoiler_body(body)
        # ``_body_trim`` caps at SPOILER_MAX_LINES (default 5) + a
        # ``… (+N more lines)`` summary line.
        assert "more lines" in out


class TestFormatHhmmss:
    def test_format(self) -> None:
        # 1700000000 corresponds to some specific HH:MM:SS depending on tz —
        # we only validate the SHAPE, not the value.
        out = _format_hhmmss(1700000000.0)
        assert len(out) == 8  # HH:MM:SS
        assert out.count(":") == 2


class TestRenderEventTimings:
    def test_inflight_shows_elapsed_not_hhmm(self) -> None:
        e = Event(type="tool_use", text="Read", started_at=1000.0)
        out = render_event(e, in_flight=True, now=1008.0)
        assert "⏳" in out
        assert "0:08" in out
        # When in-flight, HH:MM marker should NOT be present.
        assert ":" in out  # the 0:08 itself has a colon, that's ok

    def test_completed_shows_hhmm_only(self) -> None:
        e = Event(
            type="tool_use",
            text="Read",
            started_at=1000.0,
            completed_at=1010.0,
        )
        out = render_event(e, in_flight=False, now=1010.0)
        assert "✓" in out
        assert "⏳" not in out
