"""Tests for the ``CB_ARC_ALL`` (72h ↔ 14d toggle) page-skip behaviour.

When the user is on the 72h view and taps "14d", the *new* content
is the 3-14d-aged sessions appended to the end of the newest-first
list. Landing on page 0 would re-show the same 72h-aged sessions the
user just saw, hiding the older entries that motivated the tap.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.callbacks import archive as archive_cb
from ccbot.handlers.archive import PAGE_SIZE
from ccbot.handlers.callback_data import CB_ARC_ALL, CB_ARC_BACK, CB_ARC_INSPECT
from ccbot.session_models import Session


def _make_archived(idx: int, age_hours: float) -> Session:
    """A minimal archived Session record at ``age_hours`` ago."""
    now = time.time()
    return Session(
        id=f"{idx:08x}",
        name=f"sess-{idx}",
        state="archived",
        archived_at=now - age_hours * 3600,
        last_event_at=now - age_hours * 3600,
        workdir="/tmp/x",
        claude_session_id=f"c-{idx}",
    )


def _make_user(uid: int = 1) -> MagicMock:
    user = MagicMock()
    user.id = uid
    return user


def _make_query(data: str) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.answer = AsyncMock()
    query.message = MagicMock()
    return query


def _make_context(show_all: bool = False) -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {"_arc_show_all": show_all}
    return ctx


class TestArchiveAllToggle:
    @pytest.mark.asyncio
    async def test_expand_to_14d_jumps_past_the_72h_block(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two full pages of 72h-aged sessions + a few older → tapping
        '14d' opens the page where the 3-14d sessions begin (page 2)."""
        # 72h-aged: 2 full pages worth.
        recent = [_make_archived(i, age_hours=1.0) for i in range(PAGE_SIZE * 2)]
        # 3-14d-aged: 3 entries.
        older = [_make_archived(PAGE_SIZE * 2 + i, age_hours=96.0) for i in range(3)]

        def _list_archived(*, max_age_seconds=None):
            # The handler calls this twice: once for the 72h count,
            # once inside ``build_archive_page``. Honour the cap.
            cutoff = max_age_seconds or float("inf")
            now = time.time()
            return [s for s in [*recent, *older] if (now - s.archived_at) <= cutoff]

        with (
            patch.object(
                archive_cb.session_manager, "list_archived", side_effect=_list_archived
            ),
            patch(
                "ccbot.bot.callbacks.archive.build_archive_page",
                new_callable=AsyncMock,
            ) as mock_build,
            patch.object(archive_cb, "safe_edit", new_callable=AsyncMock),
        ):
            mock_build.return_value = ("text", MagicMock())
            query = _make_query(CB_ARC_ALL)
            context = _make_context(show_all=False)
            user = _make_user()
            handled = await archive_cb.handle(query, context, user)
            assert handled is True
            # build_archive_page was called with page = N_72h // PAGE_SIZE = 2
            kwargs = mock_build.call_args.kwargs
            assert kwargs["page"] == 2
            assert kwargs["show_all"] is True

    @pytest.mark.asyncio
    async def test_collapse_to_72h_returns_to_page_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tapping '72h' from the expanded view goes back to page 0 —
        the shrunk list may have invalidated the current page index."""
        with (
            patch.object(
                archive_cb.session_manager,
                "list_archived",
                return_value=[],
            ),
            patch(
                "ccbot.bot.callbacks.archive.build_archive_page",
                new_callable=AsyncMock,
            ) as mock_build,
            patch.object(archive_cb, "safe_edit", new_callable=AsyncMock),
        ):
            mock_build.return_value = ("text", MagicMock())
            query = _make_query(CB_ARC_ALL)
            context = _make_context(show_all=True)
            user = _make_user()
            await archive_cb.handle(query, context, user)
            kwargs = mock_build.call_args.kwargs
            assert kwargs["page"] == 0
            assert kwargs["show_all"] is False

    @pytest.mark.asyncio
    async def test_no_72h_sessions_expand_lands_on_page_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the 72h window is empty (everything is older), expanding
        to 14d puts the user on page 0 — the older entries ARE the only
        content, so no skip is needed."""
        older_only = [_make_archived(i, age_hours=120.0) for i in range(PAGE_SIZE + 1)]

        def _list_archived(*, max_age_seconds=None):
            cutoff = max_age_seconds or float("inf")
            now = time.time()
            return [s for s in older_only if (now - s.archived_at) <= cutoff]

        with (
            patch.object(
                archive_cb.session_manager, "list_archived", side_effect=_list_archived
            ),
            patch(
                "ccbot.bot.callbacks.archive.build_archive_page",
                new_callable=AsyncMock,
            ) as mock_build,
            patch.object(archive_cb, "safe_edit", new_callable=AsyncMock),
        ):
            mock_build.return_value = ("text", MagicMock())
            query = _make_query(CB_ARC_ALL)
            context = _make_context(show_all=False)
            user = _make_user()
            await archive_cb.handle(query, context, user)
            assert mock_build.call_args.kwargs["page"] == 0


class TestArchiveInspectBack:
    @pytest.mark.asyncio
    async def test_back_returns_to_originating_archive_page(self) -> None:
        sess = _make_archived(7, age_hours=2.0)
        context = _make_context(show_all=True)
        user = _make_user()

        with (
            patch.object(
                archive_cb.session_manager,
                "get_session",
                return_value=sess,
            ),
            patch.object(
                archive_cb,
                "_build_inspect_text",
                new_callable=AsyncMock,
                return_value="inspect",
            ),
            patch.object(
                archive_cb,
                "safe_edit",
                new_callable=AsyncMock,
            ) as safe_edit,
        ):
            inspect_query = _make_query(f"{CB_ARC_INSPECT}3:{sess.id}")
            await archive_cb.handle(inspect_query, context, user)
            inspect_keyboard = safe_edit.call_args.kwargs["reply_markup"]
            back_button = inspect_keyboard.inline_keyboard[0][-1]
            assert back_button.callback_data == f"{CB_ARC_BACK}:3"

        with (
            patch.object(
                archive_cb,
                "build_archive_page",
                new_callable=AsyncMock,
                return_value=("archive page", MagicMock()),
            ) as build,
            patch.object(archive_cb, "safe_edit", new_callable=AsyncMock),
        ):
            back_query = _make_query(f"{CB_ARC_BACK}:3")
            await archive_cb.handle(back_query, context, user)

            assert build.await_args.kwargs["page"] == 3
            assert build.await_args.kwargs["show_all"] is True

    @pytest.mark.asyncio
    async def test_legacy_back_without_page_still_returns_to_zero(self) -> None:
        with (
            patch.object(
                archive_cb,
                "build_archive_page",
                new_callable=AsyncMock,
                return_value=("archive page", MagicMock()),
            ) as build,
            patch.object(archive_cb, "safe_edit", new_callable=AsyncMock),
        ):
            await archive_cb.handle(
                _make_query(CB_ARC_BACK),
                _make_context(),
                _make_user(),
            )

            assert build.await_args.kwargs["page"] == 0
