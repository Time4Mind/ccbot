"""Outbound file delivery — a session drops a file, ``outbox_sweep`` ships it.

Pins the contract Claude relies on: drop a settled file at
``<workdir>/.ccbot-outbox/<name>`` and it goes out via the bot and gets
removed; a delivery failure leaves the file in place for the next sweep
instead of silently dropping it.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from ccbot.handlers.outbox import outbox_sweep
from ccbot.session import session_manager
from ccbot.session_models import Session


@pytest.fixture
def outbox_dir(tmp_path: Path):
    sess = Session(id="t1", name="test", workdir=str(tmp_path), state="active")
    prev = session_manager.sessions
    session_manager.sessions = {sess.id: sess}
    yield tmp_path / ".ccbot-outbox"
    session_manager.sessions = prev


def _drop(outbox: Path, name: str, data: bytes = b"hello") -> Path:
    outbox.mkdir(parents=True, exist_ok=True)
    f = outbox / name
    f.write_bytes(data)
    # Backdate mtime past the sweep's settle window.
    old = time.time() - 5
    import os

    os.utime(f, (old, old))
    return f


@pytest.mark.asyncio
async def test_document_delivered_and_removed(outbox_dir: Path) -> None:
    f = _drop(outbox_dir, "report.txt")
    bot = AsyncMock()
    sent = await outbox_sweep(bot)
    assert sent == 1
    bot.send_document.assert_awaited_once()
    assert bot.send_document.call_args.kwargs["filename"] == "report.txt"
    assert not f.exists()


@pytest.mark.asyncio
async def test_image_goes_out_as_photo(outbox_dir: Path) -> None:
    _drop(outbox_dir, "shot.png")
    bot = AsyncMock()
    await outbox_sweep(bot)
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_file_is_not_sent_yet(outbox_dir: Path) -> None:
    outbox_dir.mkdir(parents=True, exist_ok=True)
    (outbox_dir / "just-written.txt").write_bytes(b"x")  # mtime = now
    bot = AsyncMock()
    sent = await outbox_sweep(bot)
    assert sent == 0
    bot.send_document.assert_not_called()
    assert (outbox_dir / "just-written.txt").exists()


@pytest.mark.asyncio
async def test_failed_delivery_keeps_file_for_retry(outbox_dir: Path) -> None:
    f = _drop(outbox_dir, "report.txt")
    bot = AsyncMock()
    bot.send_document.side_effect = TelegramError("boom")
    sent = await outbox_sweep(bot)
    assert sent == 0
    assert f.exists()
