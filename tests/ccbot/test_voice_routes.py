"""Voice retries cross actual Bot API/file transport seams on alternating routes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram import Bot, Voice
from telegram.request import HTTPXRequest

from ccbot import input_media, input_recovery, telegram_media
from ccbot.config import config


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("primary", ["", "http://proxy.test:8080"])
@pytest.mark.parametrize("failed_stage", ["getFile", "download"])
async def test_voice_retries_alternate_routes_and_keep_file_route(
    monkeypatch, primary, failed_stage, fallback
):
    proxy = "http://proxy.test:8080"
    monkeypatch.setattr(config, "tg_proxy_url", primary)
    monkeypatch.setattr(config, "tg_media_proxy_url", proxy, raising=False)
    monkeypatch.delenv("TG_MEDIA_MTPROTO_FALLBACK", raising=False)
    observed = []
    failures = []
    if fallback:
        from ccbot import voice_mtproto

        monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1")
        monkeypatch.setattr(config, "tg_api_id", "123")
        monkeypatch.setattr(
            voice_mtproto, "document_location", lambda _: (2, 123, 456, b"ref")
        )

        def client(_api_id, _api_hash, route):
            async def connect():
                observed.append((route, "mtproto"))
                raise TimeoutError("simulated MTProto outage")

            return SimpleNamespace(
                connect=connect, log_out=AsyncMock(), disconnect=AsyncMock()
            )

        monkeypatch.setattr(voice_mtproto, "_client", client)

    def transport(route):
        def handle(req):
            stage = "getFile" if req.url.path.endswith("/getFile") else "download"
            observed.append((route, stage))
            if stage == failed_stage and len(failures) < 4:
                failures.append(route)
                raise httpx.ReadTimeout("simulated media outage", request=req)
            if stage == "getFile":
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "file_id": "voice",
                            "file_unique_id": "unique",
                            "file_size": 8,
                            "file_path": "voice/sample.oga",
                        },
                    },
                )
            return httpx.Response(200, content=b"OggS1234")

        return httpx.MockTransport(handle)

    monkeypatch.setattr(
        telegram_media, "telegram_transport", lambda **kw: transport(kw["proxy_url"])
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
        retry_delay=lambda *_: 0,
        sleep=AsyncMock(),
    )
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    async with HTTPXRequest(httpx_kwargs={"transport": transport(primary)}) as pool:
        bot = Bot("123:fake", request=pool, get_updates_request=pool)
        voice = Voice("voice", "unique", 1, file_size=8)
        voice.set_bot(bot)
        assert await input_media.download_voice(recovery, voice, request) == b"OggS1234"
    other = "" if primary else proxy
    expected = (
        [primary, primary, other, primary, other]
        if fallback
        else [primary, other, primary, other, primary]
    )
    if fallback:
        assert (
            observed[:2] == [(primary, "getFile"), (other, "mtproto")]
            if failed_stage == "getFile"
            else observed[:3]
            == [(primary, "getFile"), (primary, "download"), (other, "mtproto")]
        )
    assert [r for r, s in observed if s == "getFile"] == expected
    assert observed[-1] == (expected[-1], "download")
    if failed_stage == "download":
        assert [(r, s) for r, s in observed if s != "mtproto"] == [
            (r, s) for r in expected for s in ["getFile", "download"]
        ]
    assert request.state == "transcribing" and request.media_failures == 4
    assert config.tg_proxy_url == primary


@pytest.mark.asyncio
@pytest.mark.parametrize("error", ["rate_limit", "permanent", "cancel"])
async def test_non_network_outcomes_keep_route_or_close_retry_pool(monkeypatch, error):
    import asyncio
    from telegram.error import BadRequest
    from ccbot import voice_mtproto

    monkeypatch.setattr(config, "tg_proxy_url", "")
    monkeypatch.setattr(config, "tg_media_proxy_url", "http://proxy.test:8080")
    monkeypatch.delenv("TG_MEDIA_MTPROTO_FALLBACK", raising=False)
    observed, closed, delays = [], [], []
    fallback = AsyncMock()
    monkeypatch.setattr(voice_mtproto, "download_voice", fallback)

    class Transport(httpx.MockTransport):
        def __init__(self, route):
            self.route = route
            super().__init__(self.handle)

        def handle(self, req):
            if req.url.path.endswith("/getFile"):
                observed.append(self.route)
                if len(observed) == 1:
                    if error == "cancel":
                        raise httpx.ReadTimeout("simulated", request=req)
                    code = 429 if error == "rate_limit" else 400
                    return httpx.Response(
                        code,
                        json={
                            "ok": False,
                            "error_code": code,
                            "description": "retry later"
                            if code == 429
                            else "invalid file",
                            "parameters": {"retry_after": 7} if code == 429 else {},
                        },
                    )
                if error == "cancel":
                    raise asyncio.CancelledError()
                return httpx.Response(
                    200,
                    json={
                        "ok": True,
                        "result": {
                            "file_id": "voice",
                            "file_unique_id": "unique",
                            "file_path": "voice/sample.oga",
                        },
                    },
                )
            return httpx.Response(200, content=b"OggS1234")

        async def aclose(self):
            closed.append(self.route)
            await super().aclose()

    monkeypatch.setattr(
        telegram_media, "telegram_transport", lambda **kw: Transport(kw["proxy_url"])
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

    def delay(_, exc):
        seconds = getattr(exc, "retry_after", 0)
        delays.append(seconds)
        return seconds

    recovery = SimpleNamespace(
        source=lambda _: object(),
        clock=lambda: 100,
        changed=lambda _: None,
        retry_delay=delay,
        sleep=AsyncMock(),
    )
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    async with HTTPXRequest(httpx_kwargs={"transport": Transport("")}) as pool:
        bot = Bot("123:fake", request=pool, get_updates_request=pool)
        voice = Voice("voice", "unique", 1, file_size=8)
        voice.set_bot(bot)
        if error == "rate_limit":
            assert (
                await input_media.download_voice(recovery, voice, request)
                == b"OggS1234"
            )
            recovery.sleep.assert_awaited_once_with(7)
            assert (
                observed == ["", ""] and delays == [7] and request.media_failures == 0
            )
        else:
            with pytest.raises(
                asyncio.CancelledError if error == "cancel" else BadRequest
            ):
                await input_media.download_voice(recovery, voice, request)
            assert observed == (
                ["", "http://proxy.test:8080"] if error == "cancel" else [""]
            )
    assert set(closed) == set(observed)
    fallback.assert_not_awaited()
