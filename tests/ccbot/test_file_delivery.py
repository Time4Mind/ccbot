from __future__ import annotations

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ccbot.file_actions import RemoteFileReference
from ccbot.file_delivery import FileDeliveryManager, SubmitResult


@pytest.mark.asyncio
async def test_manager_coalesces_same_file_and_bounds_user_and_global_work(
    tmp_path: Path,
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    other_path = tmp_path / "other.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    other_path.write_text("other", encoding="utf-8")
    upload_started = asyncio.Event()
    release_first = asyncio.Event()
    uploads: list[tuple[int, str]] = []

    async def send_document(**kwargs):
        uploads.append((kwargs["chat_id"], kwargs["filename"]))
        if kwargs["filename"] == "first.txt":
            upload_started.set()
            await release_first.wait()

    bot = SimpleNamespace(send_document=AsyncMock(side_effect=send_document))
    manager = FileDeliveryManager(global_limit=1, staging_dir=tmp_path / "staging")

    assert (
        manager.submit(bot=bot, user_id=1, delivery_key="same", reference=first_path)
        is SubmitResult.ACCEPTED
    )
    assert (
        manager.submit(bot=bot, user_id=1, delivery_key="same", reference=first_path)
        is SubmitResult.DUPLICATE
    )
    assert (
        manager.submit(bot=bot, user_id=1, delivery_key="second", reference=second_path)
        is SubmitResult.USER_BUSY
    )
    assert (
        manager.submit(bot=bot, user_id=2, delivery_key="other", reference=other_path)
        is SubmitResult.ACCEPTED
    )
    await upload_started.wait()
    await asyncio.sleep(0)
    assert uploads == [(1, "first.txt")]

    release_first.set()
    await manager.wait_for_idle()

    assert uploads == [(1, "first.txt"), (2, "other.txt")]


@pytest.mark.asyncio
async def test_shutdown_cancels_remote_fetch_and_removes_staged_file(
    monkeypatch, tmp_path: Path
) -> None:
    fetch_started = asyncio.Event()
    never_finish = asyncio.Event()

    async def download_to(_node, _session, _path, destination, **_kwargs):
        destination.write(b"partial")
        destination.flush()
        fetch_started.set()
        await never_finish.wait()

    runtime = SimpleNamespace(download_session_file_to=download_to)
    monkeypatch.setattr("ccbot.file_delivery.get_node_runtime", lambda _node: runtime)
    staging = tmp_path / "staging"
    manager = FileDeliveryManager(staging_dir=staging)
    reference = RemoteFileReference(
        node_id="worker-a",
        session_id="routing-1",
        path="/worker/report.zip",
        name="report.zip",
        size=7,
        version="7:1",
    )
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())

    assert (
        manager.submit(bot=bot, user_id=1, delivery_key="file", reference=reference)
        is SubmitResult.ACCEPTED
    )
    await fetch_started.wait()
    staged_files = list(staging.iterdir())
    assert len(staged_files) == 1
    assert stat.S_IMODE(staging.stat().st_mode) == 0o700
    assert stat.S_IMODE(staged_files[0].stat().st_mode) == 0o600

    await manager.shutdown(timeout=0)
    await manager.wait_for_idle()

    assert list(staging.iterdir()) == []
    bot.send_document.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["changed", "timeout"])
async def test_remote_fetch_failure_sends_one_message_and_no_document(
    monkeypatch, tmp_path: Path, failure_mode: str
) -> None:
    async def download_to(*_args, **_kwargs):
        if failure_mode == "timeout":
            await asyncio.Event().wait()
        raise ValueError("changed")

    runtime = SimpleNamespace(download_session_file_to=download_to)
    monkeypatch.setattr("ccbot.file_delivery.get_node_runtime", lambda _node: runtime)
    manager = FileDeliveryManager(worker_timeout=0.01, staging_dir=tmp_path / "staging")
    reference = RemoteFileReference(
        node_id="worker-a",
        session_id="routing-1",
        path="/worker/report.zip",
        name="report.zip",
        size=7,
        version="7:1",
    )
    bot = SimpleNamespace(send_document=AsyncMock(), send_message=AsyncMock())

    manager.submit(bot=bot, user_id=1, delivery_key="file", reference=reference)
    await manager.wait_for_idle()

    bot.send_document.assert_not_awaited()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_upload_timeout_is_not_retried(tmp_path: Path) -> None:
    path = tmp_path / "result.zip"
    path.write_bytes(b"payload")

    async def send_document(**_kwargs):
        await asyncio.Event().wait()

    bot = SimpleNamespace(
        send_document=AsyncMock(side_effect=send_document), send_message=AsyncMock()
    )
    manager = FileDeliveryManager(
        telegram_timeout=0.01, staging_dir=tmp_path / "staging"
    )

    manager.submit(bot=bot, user_id=1, delivery_key="file", reference=path)
    await manager.wait_for_idle()

    bot.send_document.assert_awaited_once()
    bot.send_message.assert_awaited_once()
