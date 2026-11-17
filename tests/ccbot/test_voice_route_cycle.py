"""Ordered route cycling through real Bot API transport and durable recovery."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from telegram import Bot, Voice
from telegram.request import HTTPXRequest
from ccbot import input_media, input_recovery, telegram_media, voice_mtproto
from ccbot.config import config

P1 = "http://proxy-one.test:8080"
P2 = "http://proxy-two.test:8080"
EXPECTED = ["", P1, P2, "", P1, P2, "", P1, P2, ""]


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize(
    "outcome", ["success10", "exhausted", "rate_limit", "cancel_restart"]
)
async def test_direct_first_cycles_two_proxies_with_ten_total_attempts(
    monkeypatch, tmp_path, fallback, outcome
):
    monkeypatch.setattr(config, "tg_proxy_url", "http://polling-only.test:8080")
    monkeypatch.setattr(config, "tg_media_proxy_url", P1)
    monkeypatch.setattr(config, "tg_media_proxy_urls", (P1, P2), raising=False)
    monkeypatch.setenv("TG_MEDIA_MTPROTO_FALLBACK", "1" if fallback else "0")
    monkeypatch.setattr(config, "tg_api_id", "123")
    observed = []
    waits = []

    def transport(route):
        def handle(req):
            if req.url.path.endswith("/getFile"):
                observed.append(route)
                if outcome == "cancel_restart" and len(observed) == 2:
                    raise asyncio.CancelledError()
                if outcome == "rate_limit" and len(observed) == 1:
                    return httpx.Response(
                        429,
                        json={
                            "ok": False,
                            "description": "wait",
                            "error_code": 429,
                            "parameters": {"retry_after": 7},
                        },
                    )
                if len(observed) == 10 and outcome != "exhausted":
                    return httpx.Response(
                        200,
                        json={
                            "ok": True,
                            "result": {
                                "file_id": "voice",
                                "file_unique_id": "unique",
                                "file_path": "voice/sample.oga",
                                "file_size": 8,
                            },
                        },
                    )
                raise httpx.ReadTimeout("simulated outage", request=req)
            return httpx.Response(200, content=b"OggS1234")

        return httpx.MockTransport(handle)

    monkeypatch.setattr(
        telegram_media, "telegram_transport", lambda **kw: transport(kw["proxy_url"])
    )
    monkeypatch.setattr(
        voice_mtproto, "document_location", lambda _: (2, 123, 456, b"ref")
    )

    def mtproto(_id, _hash, route):
        async def connect():
            observed.append(route)
            if outcome == "cancel_restart" and len(observed) == 2:
                raise asyncio.CancelledError()
            raise TimeoutError("simulated")

        return SimpleNamespace(
            connect=connect, log_out=AsyncMock(), disconnect=AsyncMock()
        )

    monkeypatch.setattr(voice_mtproto, "_client", mtproto)
    request = input_recovery.InputRequest("42:1", 42, 1, "s", "@s", "voice", {})
    request.media_attempts = 0
    recovery = input_recovery.InputRecovery(tmp_path / "journal.json")
    recovery.records[request.id] = request
    recovery.source = lambda _: object()
    recovery.clock = lambda: 100
    recovery.retry_delay = lambda _, exc: getattr(exc, "retry_after", 0)

    async def sleep(seconds):
        waits.append(seconds)

    recovery.sleep = sleep
    monkeypatch.setattr(input_recovery, "input_recovery", recovery)
    async with HTTPXRequest(
        httpx_kwargs={"transport": transport(config.tg_proxy_url), "trust_env": False}
    ) as pool:
        bot = Bot("123:fake", request=pool, get_updates_request=pool)
        voice = Voice("voice", "unique", 1, file_size=8)
        voice.set_bot(bot)
        result = None
        try:
            result = await input_media.download_voice(recovery, voice, request)
        except asyncio.CancelledError:
            assert outcome == "cancel_restart"
            resumed = input_recovery.InputRecovery(recovery.path)
            resumed.load()
            request = resumed.records[request.id]
            assert request.media_attempts == 2
            resumed.source = recovery.source
            resumed.clock = recovery.clock
            resumed.retry_delay = recovery.retry_delay
            resumed.sleep = recovery.sleep
            recovery = resumed
            monkeypatch.setattr(input_recovery, "input_recovery", recovery)
            result = await input_media.download_voice(recovery, voice, request)
        except ValueError:
            pass
        if outcome == "exhausted":
            with pytest.raises(ValueError, match="10 попыток"):
                await input_media.download_voice(recovery, voice, request)
    assert observed == EXPECTED
    if outcome == "exhausted":
        assert (
            result is None
            and request.state == "failed"
            and "10" in request.notice
            and "/requests_retry_1" in request.notice
        )
    else:
        assert result == b"OggS1234" and request.state == "transcribing"
    assert request.media_attempts == 10
    if outcome == "rate_limit":
        assert waits == [7]
    restarted = input_recovery.InputRecovery(recovery.path)
    restarted.load()
    assert restarted.records[request.id].media_attempts == 10
    # New budget data must stay outside legacy InputRequest constructor rows.
    raw = json.loads(recovery.path.read_text())
    assert "media_attempts" not in raw["requests"][0]
    assert raw["media_recovery"][request.id]["media_attempts"] == 10
