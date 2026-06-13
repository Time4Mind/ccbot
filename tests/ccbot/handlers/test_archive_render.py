"""Regression tests for ``build_archive_page`` body formatting.

The leading ``N.`` on each row used to be parsed by the rich-message
CommonMark renderer as a fresh ordered-list item per row (the
2-space-indented blurb / workdir / goal continuations are shy of the
3-space margin CommonMark requires, so each item ended its own list).
Telegram then renumbered every item from 1, so on page 2 the inline
buttons labelled 6-10 lined up next to body rows showing 1-5.

Escaping the dot (``N\\.``) keeps the source index visible literally
and stops the list-parse / renumber chain. The body must remain
parse-safe across pages.
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
            return_value="",
        ),
    ):
        yield sessions


class TestArchivePageNumbering:
    @pytest.mark.asyncio
    async def test_page2_indices_escape_the_dot(self, many_archived) -> None:
        """Page-2 rows must carry ``6\\. ... 10\\.`` so the rich parser
        can't reflow them into a new ordered list starting at 1."""
        text, _ = await build_archive_page(
            page=1,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        for idx in range(PAGE_SIZE + 1, PAGE_SIZE * 2 + 1):
            assert f"{idx}\\. " in text, f"row {idx} missing escaped dot"
            assert f"\n{idx}. " not in text, f"row {idx} kept the bare dot"

    @pytest.mark.asyncio
    async def test_page1_also_escapes_for_consistency(self, many_archived) -> None:
        text, _ = await build_archive_page(
            page=0,
            lookback_seconds=None,
            show_all=True,
            user_id=1,
        )
        for idx in range(1, PAGE_SIZE + 1):
            assert f"{idx}\\. " in text

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
        # Page-2 buttons are labelled 6-10 with the plain dot.
        assert any(lbl.startswith("6. ") for lbl in labels)
        assert any(lbl.startswith("10. ") for lbl in labels)
