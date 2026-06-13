"""Regression tests for ``build_archive_page`` body formatting.

Two CommonMark traps fired in sequence under the rich-message renderer:

1. Single ``\\n`` between rows / sub-rows is a *soft* line break in
   CommonMark — the renderer collapses it to a space, so the whole
   page came out as one wall-of-text paragraph (verified live, see
   the 2026-06-13 phone screenshot at .ccbot-inbox/1781353376-…).
   The fix is paragraph breaks ``\\n\\n`` between rows and hard
   breaks ``  \\n`` (two trailing spaces) within a row's sub-lines.

2. A bare ``N.`` at line start would be parsed as a fresh ordered-list
   marker per row (the 2-space-indented continuations are shy of the
   3-space margin CommonMark needs), and Telegram would renumber each
   list from 1 — page-2 buttons labelled 6-10 next to body rows 1-5.
   Wrapping the index in ``**N.**`` shifts the line start from a digit
   to ``*`` so the marker can't trigger. (A backslash escape ``N\\.``
   would do the same job, but Telegram's rich parser doesn't honour
   the escape and leaks the backslash to the chat — verified live on
   PR #112.)
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from ccbot.handlers.archive import PAGE_SIZE, build_archive_page
from ccbot.session_models import Session


def _make_archived(idx: int) -> Session:
    return Session(
        id=f"{idx:08x}",
        name=f"sess-{idx}",
        state="archived",
        archived_at=time.time() - 3600,
        last_event_at=time.time() - 3600,
        workdir="/tmp/x",
        claude_session_id=f"c-{idx}",
    )


@pytest.fixture
def many_archived():
    sessions = [_make_archived(i) for i in range(PAGE_SIZE * 3)]
    with (
        patch(
            "ccbot.handlers.archive.session_manager.list_archived",
            return_value=sessions,
        ),
        patch(
            "ccbot.handlers.archive._archive_blurb",
            return_value="a short blurb for the row",
        ),
    ):
        yield sessions


class TestArchivePageNumbering:
    @pytest.mark.asyncio
    async def test_page2_indices_bold_wrapped(self, many_archived) -> None:
        """Page-2 rows must carry ``**6.** ... **10.**`` so the line
        starts with ``*`` rather than a digit — CommonMark can't read
        it as an ordered-list marker and Telegram can't renumber it."""
        text, _ = await build_archive_page(
            page=1,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        for idx in range(PAGE_SIZE + 1, PAGE_SIZE * 2 + 1):
            assert f"**{idx}.** " in text, f"row {idx} missing bold wrap"
            # Bare ``N. `` at line start would re-trigger the list parse.
            assert f"\n{idx}. " not in text, f"row {idx} kept a bare dot"
        # And no leaked backslashes from the prior escape attempt.
        assert "\\." not in text

    @pytest.mark.asyncio
    async def test_page1_also_bold_wrapped_for_consistency(self, many_archived) -> None:
        text, _ = await build_archive_page(
            page=0,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        for idx in range(1, PAGE_SIZE + 1):
            assert f"**{idx}.** " in text

    @pytest.mark.asyncio
    async def test_button_labels_keep_plain_dot(self, many_archived) -> None:
        """Inline-button labels are not markdown — keep the bare dot."""
        _text, kb = await build_archive_page(
            page=1,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        labels = [btn.text for row in kb.inline_keyboard for btn in row]
        # Page-2 buttons cover indices PAGE_SIZE+1 … PAGE_SIZE*2.
        assert any(lbl.startswith(f"{PAGE_SIZE + 1}. ") for lbl in labels)
        assert any(lbl.startswith(f"{PAGE_SIZE * 2}. ") for lbl in labels)


class TestArchivePageLineBreaks:
    @pytest.mark.asyncio
    async def test_rows_separated_by_paragraph_break(self, many_archived) -> None:
        """Rows must be separated by a blank line — single ``\\n`` is a
        soft break and CommonMark collapses the entire page into one
        run-on paragraph (the 2026-06-13 phone-screenshot bug)."""
        text, _ = await build_archive_page(
            page=0,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        # Each row's leading marker must be preceded by ``\n\n``.
        for idx in range(1, PAGE_SIZE + 1):
            marker = f"**{idx}.**"
            assert marker in text
            pos = text.index(marker)
            assert text[pos - 2 : pos] == "\n\n", (
                f"row {idx} not preceded by a paragraph break"
            )

    @pytest.mark.asyncio
    async def test_sublines_use_hard_break(self, many_archived) -> None:
        """Within a row, sub-lines (blurb / workdir / goal) join with
        ``  \\n`` — two trailing spaces force a hard line break in
        CommonMark, instead of the soft break that collapses to a space."""
        text, _ = await build_archive_page(
            page=0,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        # At least one hard break per row (we seeded a blurb and a workdir).
        assert text.count("  \n") >= PAGE_SIZE

    @pytest.mark.asyncio
    async def test_no_two_space_indent_remains(self, many_archived) -> None:
        """The old MD V2-era 2-space indent on sub-lines is gone — leading
        whitespace inside a paragraph would render as literal spaces in
        the rich parser, not as visual indent."""
        text, _ = await build_archive_page(
            page=0,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        # ``\n  `` (line-start + 2 spaces of content) is the old pattern;
        # the new layout uses ``  \n`` (trailing spaces before the break).
        assert "\n  " not in text


class TestDisplayName:
    def test_kebab_renders_as_spaces(self) -> None:
        from ccbot.handlers.archive import _display_name

        sess = Session(
            id="abcd",
            name="archive-pagination-fix",
            state="archived",
        )
        assert _display_name(sess) == "archive pagination fix"

    def test_fallback_to_id_when_no_name(self) -> None:
        from ccbot.handlers.archive import _display_name

        sess = Session(id="abcd1234", name="", state="archived")
        assert _display_name(sess) == "abcd1234"

    def test_directory_name_passes_through_with_spaces(self) -> None:
        from ccbot.handlers.archive import _display_name

        sess = Session(id="abcd", name="workdir-6", state="archived")
        # ``-N`` collision suffix becomes ``workdir 6`` — still readable.
        assert _display_name(sess) == "workdir 6"


class TestCleanUserMsg:
    def test_collapses_whitespace(self) -> None:
        from ccbot.handlers.archive import _clean_user_msg

        assert _clean_user_msg("line1\n\nline2   line3") == "line1 line2 line3"

    def test_drops_leading_slash_command(self) -> None:
        from ccbot.handlers.archive import _clean_user_msg

        assert _clean_user_msg("/resume real ask") == "real ask"

    def test_strips_backticks_and_spaces(self) -> None:
        from ccbot.handlers.archive import _clean_user_msg

        assert _clean_user_msg("` something `") == "something"

    def test_does_not_truncate(self) -> None:
        from ccbot.handlers.archive import _clean_user_msg

        long_text = "a" * 500
        assert _clean_user_msg(long_text) == long_text


class TestArchiveBlurbCollectsUserMessages:
    """``_archive_blurb`` reads the first 1-3 user messages from the
    JSONL, accumulating until the soft budget bites."""

    @pytest.fixture(autouse=True)
    def reset_blurb_cache(self):
        from ccbot.handlers import archive

        archive._BLURB_CACHE.clear()
        yield
        archive._BLURB_CACHE.clear()

    @pytest.mark.asyncio
    async def test_three_short_messages_all_included(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ccbot.handlers import archive

        sess = Session(
            id="s1", name="", state="archived",
            claude_session_id="cs-1", workdir="/tmp/x",
        )
        monkeypatch.setattr(
            archive,
            "_collect_user_messages",
            _fake_collect("first ask  \nsecond ask  \nthird ask"),
        )
        out = await archive._archive_blurb(sess)
        assert "first ask" in out
        assert "second ask" in out
        assert "third ask" in out

    @pytest.mark.asyncio
    async def test_no_haiku_no_spoiler(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Blurb is the user's verbatim words — no ``||spoiler||`` marks,
        no ``…``, no translation."""
        from ccbot.handlers import archive

        sess = Session(
            id="s2", name="", state="archived",
            claude_session_id="cs-2", workdir="/tmp/x",
        )
        verbatim = (
            "Найди баги в auth.py — особенно вокруг refresh-token rotation"
        )
        monkeypatch.setattr(
            archive, "_collect_user_messages", _fake_collect(verbatim),
        )
        out = await archive._archive_blurb(sess)
        assert out == verbatim
        assert "||" not in out
        assert "…" not in out

    @pytest.mark.asyncio
    async def test_cache_hit_skips_jsonl_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ccbot.handlers import archive

        sess = Session(
            id="s3", name="", state="archived",
            claude_session_id="cs-3", workdir="/tmp/x",
        )
        # Pre-seed the cache.
        archive._BLURB_CACHE["cs-3"] = "cached-from-disk"
        calls: list[Session] = []

        async def _should_not_fire(s: Session) -> str:
            calls.append(s)
            return "live-scan"

        monkeypatch.setattr(archive, "_collect_user_messages", _should_not_fire)
        out = await archive._archive_blurb(sess)
        assert out == "cached-from-disk"
        assert calls == []

    @pytest.mark.asyncio
    async def test_long_first_message_included_whole(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A single first message that already exceeds the soft budget
        is still emitted whole — the user's own words trump truncation."""
        from ccbot.handlers import archive

        sess = Session(
            id="s4", name="", state="archived",
            claude_session_id="cs-4", workdir="/tmp/x",
        )
        # Simulate what ``_collect_user_messages`` would return for one
        # very long message — included whole, no follow-up messages.
        long_msg = "a" * 600
        monkeypatch.setattr(
            archive, "_collect_user_messages", _fake_collect(long_msg),
        )
        out = await archive._archive_blurb(sess)
        assert out == long_msg


def _fake_collect(value: str):
    async def _collect(_sess):
        return value

    return _collect
