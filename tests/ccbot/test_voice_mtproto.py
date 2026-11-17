"""Bot-only fallback preserves voice identity and the bounded FIFO contract."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from telegram.error import TimedOut
from ccbot import input_media


@pytest.mark.asyncio
async def test_network_failure_uses_alternate_download_before_repeating_getfile(
    monkeypatch,
):
    from ccbot import voice_mtproto

    monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1")
    fallback = AsyncMock(return_value=b"OggS voice bytes")
    monkeypatch.setattr(voice_mtproto, "download_voice", fallback)
    voice = SimpleNamespace(
        get_file=AsyncMock(
            side_effect=[
                TimedOut(),
                SimpleNamespace(
                    download_as_bytearray=AsyncMock(return_value=b"normal")
                ),
            ]
        ),
        file_id="voice",
    )
    request = SimpleNamespace(
        id="42:1",
        message_id=1,
        session_id="s",
        kind="voice",
        state="accepted",
        stage="",
        media_failures=0,
        retry_at=0,
        attempts=0,
    )
    recovery = SimpleNamespace(
        source=lambda _: object(),
        clock=lambda: 100,
        changed=lambda _: None,
        retry_delay=lambda *_: 1,
        sleep=AsyncMock(),
        fail=AsyncMock(),
    )
    from ccbot import input_recovery

    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    assert (
        await input_media.download_voice(recovery, voice, request)
        == b"OggS voice bytes"
    )
    assert request.state == "transcribing"
    assert voice.get_file.await_count == 1
    fallback.assert_awaited_once_with(voice)
    recovery.sleep.assert_not_awaited()


def voice_id():
    import base64
    import struct

    # A v4 voice document: DC2, reference 'ref', document123, access456.
    raw = (
        struct.pack("<ii", 3 | (1 << 25), 2)
        + b"\x03ref"
        + struct.pack("<qq", 123, 456)
        + bytes([30, 4])
    )
    encoded = bytearray()
    index = 0
    while index < len(raw):
        if raw[index]:
            encoded.append(raw[index])
            index += 1
        else:
            end = index
            while end < len(raw) and not raw[end]:
                end += 1
            encoded.extend((0, end - index))
            index = end
    return base64.urlsafe_b64encode(encoded).decode().rstrip("=")


@pytest.fixture
def alternate(monkeypatch):
    from ccbot import voice_mtproto as module

    monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1")
    from ccbot.config import config

    monkeypatch.setattr(config, "tg_api_id", "123")
    monkeypatch.setattr(config, "tg_api_hash", "test-api-hash")
    client = SimpleNamespace(
        connect=AsyncMock(),
        sign_in=AsyncMock(),
        get_me=AsyncMock(return_value=SimpleNamespace(bot=True, id=123)),
        log_out=AsyncMock(return_value=True),
        disconnect=AsyncMock(),
    )
    chunks = [b"OggS", b"1234"]

    async def download(location, **kwargs):
        assert location == (123, 456, b"ref")
        assert kwargs == {"dc_id": 2, "file_size": 8}
        for chunk in chunks:
            yield chunk

    client.iter_download = download
    monkeypatch.setattr(module, "_client", lambda *_: client)
    monkeypatch.setattr(module, "_location", lambda *args: args)
    voice = SimpleNamespace(
        file_id=voice_id(),
        file_size=8,
        get_bot=lambda: SimpleNamespace(token="123:fake"),
    )
    return SimpleNamespace(module=module, client=client, voice=voice, chunks=chunks)


@pytest.mark.asyncio
async def test_alternate_download_verifies_identity_bytes_and_releases_authorization(
    alternate,
):
    assert await alternate.module.download_voice(alternate.voice) == b"OggS1234"
    alternate.client.log_out.assert_awaited_once()
    alternate.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    ["wrong_bot", "size_short", "size_long", "invalid_audio", "invalid_identifier"],
)
async def test_alternate_rejects_unverified_download(alternate, case):
    if case == "wrong_bot":
        alternate.client.get_me.return_value.id = 456
    elif case == "size_short":
        alternate.chunks[:] = [b"OggS"]
    elif case == "size_long":
        alternate.chunks[:] = [b"OggS123456789"]
    elif case == "invalid_audio":
        alternate.chunks[:] = [b"bad!1234"]
    else:
        alternate.voice.file_id = "invalid"
    assert await alternate.module.download_voice(alternate.voice) is None
    if case != "invalid_identifier":
        alternate.client.log_out.assert_awaited_once()
        alternate.client.disconnect.assert_awaited_once()
    else:
        alternate.client.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_fallback_never_authorizes(alternate, monkeypatch):
    monkeypatch.delenv("TG_MEDIA_MTPROTO_FALLBACK")
    assert await alternate.module.download_voice(alternate.voice) is None
    alternate.client.connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_download_releases_auth_and_propagates(alternate):
    import asyncio

    started = asyncio.Event()

    async def pending(*args, **kwargs):
        started.set()
        await asyncio.Future()
        yield b""

    alternate.client.iter_download = pending
    task = asyncio.create_task(alternate.module.download_voice(alternate.voice))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    alternate.client.log_out.assert_awaited_once()
    alternate.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_alternate_timeout_returns_to_existing_retries(alternate, monkeypatch):
    import asyncio

    monkeypatch.setattr(alternate.module, "_TIMEOUT", 0.01)

    async def pending(*args, **kwargs):
        await asyncio.Future()
        yield b""

    alternate.client.iter_download = pending
    assert await alternate.module.download_voice(alternate.voice) is None
    alternate.client.log_out.assert_awaited_once()
    alternate.client.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_alternate_is_attempted_once_then_fifo_failure_is_preserved(
    monkeypatch,
):
    from contextlib import asynccontextmanager
    from ccbot import input_recovery, voice_mtproto

    monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1")
    fallback = AsyncMock(return_value=None)
    monkeypatch.setattr(voice_mtproto, "download_voice", fallback)
    voice = SimpleNamespace(file_id="voice", get_file=AsyncMock(side_effect=TimedOut()))

    @asynccontextmanager
    async def fresh(_voice):
        yield voice

    monkeypatch.setattr(input_media, "fresh_media_bot", fresh)
    request = SimpleNamespace(
        id="42:1",
        message_id=1,
        session_id="s",
        kind="voice",
        state="accepted",
        stage="",
        media_failures=0,
        retry_at=0,
        attempts=0,
        media_deferred=False,
    )

    def fail(r, text):
        r.notice = text
        r.state = "failed"

    recovery = SimpleNamespace(
        source=lambda _: object(),
        clock=lambda: 100,
        changed=lambda _: None,
        retry_delay=lambda *_: 1,
        sleep=AsyncMock(),
        fail=fail,
    )
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    with pytest.raises(ValueError, match="следующие сообщения продолжат обработку"):
        await input_media.download_voice(recovery, voice, request)
    fallback.assert_awaited_once_with(voice)
    assert voice.get_file.await_count == 5
    assert request.media_deferred and request.state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["permanent", "rate_limit"])
async def test_non_network_errors_do_not_trigger_alternate(monkeypatch, kind):
    from telegram.error import BadRequest, RetryAfter
    from ccbot import input_recovery, voice_mtproto

    monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1")
    fallback = AsyncMock(return_value=b"wrong")
    monkeypatch.setattr(voice_mtproto, "download_voice", fallback)
    failure = BadRequest("unavailable") if kind == "permanent" else RetryAfter(1)
    voice = SimpleNamespace(
        get_file=AsyncMock(
            side_effect=[
                failure,
                SimpleNamespace(
                    download_as_bytearray=AsyncMock(return_value=b"normal")
                ),
            ]
        )
    )
    request = SimpleNamespace(
        id="42:1",
        message_id=1,
        session_id="s",
        kind="voice",
        state="accepted",
        stage="",
        media_failures=0,
        retry_at=0,
        attempts=0,
    )
    recovery = SimpleNamespace(
        source=lambda _: object(),
        clock=lambda: 100,
        changed=lambda _: None,
        retry_delay=lambda *_: 1,
        sleep=AsyncMock(),
    )
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    if kind == "permanent":
        with pytest.raises(BadRequest):
            await input_media.download_voice(recovery, voice, request)
    else:
        assert await input_media.download_voice(recovery, voice, request) == b"normal"
    fallback.assert_not_awaited()


def test_media_credentials_are_captured_without_leaking_to_agents(
    monkeypatch, tmp_path
):
    import os
    from ccbot.config import Config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake")
    monkeypatch.setenv("ALLOWED_USERS", "42")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "test-api-hash")
    cfg = Config()
    assert cfg.tg_api_id == "123" and cfg.tg_api_hash == "test-api-hash"
    assert "TG_API_ID" not in os.environ and "TG_API_HASH" not in os.environ
