"""``ccbot send-file`` — on-demand outbound delivery.

Pins the two contracts Claude relies on: chat-target resolution precedence
(``--chat-id`` > ``$CCBOT_CHAT_ID`` > broadcast to all allowed users) and
that a per-chat delivery failure is reported, not swallowed, so the
calling Bash tool call actually sees it went wrong.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from ccbot.send_file import (
    _send_via_daemon,
    deliver,
    resolve_chat_ids,
    send_file_relay_dir,
    send_file_relay_loop,
)


def test_explicit_chat_id_wins() -> None:
    assert resolve_chat_ids(111, "222", {333}) == [111]


def test_env_chat_id_used_when_no_cli_override() -> None:
    assert resolve_chat_ids(None, "222", {333}) == [222]


def test_falls_back_to_broadcast() -> None:
    assert resolve_chat_ids(None, None, {333, 111}) == [111, 333]


def test_no_target_is_empty_list() -> None:
    assert resolve_chat_ids(None, None, set()) == []


def test_default_relay_is_in_sandbox_writable_tmp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path / "deployment"))
    relay_dir = send_file_relay_dir()
    assert relay_dir.parent == Path("/tmp")
    assert relay_dir.name.startswith(f"ccbot-send-file-{os.getuid()}-")


@pytest.mark.asyncio
async def test_document_delivered(tmp_path: Path) -> None:
    f = tmp_path / "report.txt"
    f.write_text("hello")
    bot = AsyncMock()
    ok = await deliver(bot, f, None, [111])
    assert ok is True
    bot.send_document.assert_awaited_once()
    assert bot.send_document.call_args.kwargs["filename"] == "report.txt"


@pytest.mark.asyncio
async def test_image_goes_out_as_photo(tmp_path: Path) -> None:
    f = tmp_path / "shot.png"
    f.write_bytes(b"\x89PNG")
    bot = AsyncMock()
    await deliver(bot, f, None, [111])
    bot.send_photo.assert_awaited_once()
    bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_failure_on_one_chat_is_reported(tmp_path: Path) -> None:
    f = tmp_path / "report.txt"
    f.write_text("hello")
    bot = AsyncMock()
    bot.send_document.side_effect = [None, TelegramError("boom")]
    ok = await deliver(bot, f, None, [111, 222])
    assert ok is False
    assert bot.send_document.await_count == 2


@pytest.mark.asyncio
async def test_daemon_sends_file_and_returns_result(tmp_path: Path) -> None:
    f = tmp_path / "report.txt"
    f.write_text("hello")
    bot = AsyncMock()
    relay_dir = tmp_path / "relay"
    relay_task = asyncio.create_task(
        send_file_relay_loop(bot, relay_dir=relay_dir, allowed_users={111})
    )
    await asyncio.sleep(0)
    try:
        ok = await _send_via_daemon(f, "caption", [111], relay_dir)
    finally:
        relay_task.cancel()
        await asyncio.gather(relay_task, return_exceptions=True)

    assert ok is True
    bot.send_document.assert_awaited_once()
    assert bot.send_document.call_args.kwargs["chat_id"] == 111
    assert bot.send_document.call_args.kwargs["caption"] == "caption"


@pytest.mark.asyncio
async def test_daemon_rejects_chat_outside_allowed_users(tmp_path: Path) -> None:
    f = tmp_path / "report.txt"
    f.write_text("hello")
    bot = AsyncMock()
    relay_dir = tmp_path / "relay"
    relay_task = asyncio.create_task(
        send_file_relay_loop(bot, relay_dir=relay_dir, allowed_users={111})
    )
    await asyncio.sleep(0)
    try:
        ok = await _send_via_daemon(f, None, [222], relay_dir)
    finally:
        relay_task.cancel()
        await asyncio.gather(relay_task, return_exceptions=True)

    assert ok is False
    bot.send_document.assert_not_awaited()
