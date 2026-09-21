"""Archive callback compatibility and inspect/back behavior."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ccbot.bot.callbacks import archive as archive_cb
from ccbot.handlers.callback_data import CB_ARC_BACK, CB_ARC_INSPECT, CB_ARC_RESTORE
from ccbot.session_models import Session
from ccbot.node_models import Node


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


def _make_context() -> MagicMock:
    ctx = MagicMock()
    ctx.user_data = {}
    return ctx


class TestArchiveInspectBack:
    @pytest.mark.asyncio
    async def test_restore_callback_is_acknowledged_before_worker_finishes(
        self,
    ) -> None:
        sess = _make_archived(8, age_hours=2.0)
        entered = asyncio.Event()
        release = asyncio.Event()
        surface_updated = asyncio.Event()

        async def blocked_restore(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return False, "worker startup timeout"

        async def update_surface(*_args, **_kwargs):
            surface_updated.set()

        query = _make_query(f"{CB_ARC_RESTORE}{sess.id}")
        with (
            patch.object(archive_cb.session_manager, "get_session", return_value=sess),
            patch.object(archive_cb, "restore_session", side_effect=blocked_restore),
            patch.object(archive_cb, "safe_edit", side_effect=update_surface) as edit,
        ):
            task = asyncio.create_task(
                archive_cb.handle(query, _make_context(), _make_user())
            )
            try:
                await entered.wait()
                await asyncio.sleep(0)

                assert task.done()
                assert task.result() is True
                query.answer.assert_awaited_once()
                edit.assert_not_awaited()

                release.set()
                await asyncio.wait_for(surface_updated.wait(), timeout=1)
            finally:
                release.set()
                await task

        assert "worker startup timeout" in edit.await_args.args[1]

    @pytest.mark.asyncio
    async def test_remote_inspect_identifies_source_node(self) -> None:
        sess = _make_archived(7, age_hours=2.0)
        sess.node_id = "worker-a"
        with (
            patch.object(archive_cb, "render_archived_card_pages", return_value=None),
            patch.object(
                archive_cb, "render_session_preview", return_value="session preview"
            ),
            patch.object(
                archive_cb.session_manager,
                "get_node",
                return_value=Node(id="worker-a", display_name="Worker A"),
            ),
        ):
            text = await archive_cb._build_inspect_text(sess, 1)

        assert "Node: *Worker A*" in text or "Нода: *Worker A*" in text

    @pytest.mark.asyncio
    async def test_back_returns_to_originating_archive_page(self) -> None:
        sess = _make_archived(7, age_hours=2.0)
        context = _make_context()
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
            assert build.await_args.kwargs["show_all"] is False

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
