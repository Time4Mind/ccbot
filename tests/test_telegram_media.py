"""Media recovery uses its own transport and the original Bot API route."""

from types import SimpleNamespace

import httpx
import pytest
from telegram import Bot

from ccbot import telegram_media


@pytest.mark.asyncio
async def test_fresh_media_pool_downloads_and_closes_without_touching_original(
    monkeypatch,
):
    transports = []
    requests = []

    class Transport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    def handle(request):
        requests.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": {
                        "file_id": "voice",
                        "file_unique_id": "unique",
                        "file_path": "voices/one.ogg",
                    },
                },
            )
        return httpx.Response(200, content=b"ogg payload")

    def transport(**_kwargs):
        result = Transport(handle)
        transports.append(result)
        return result

    monkeypatch.setattr(telegram_media, "telegram_transport", transport)
    original = Bot(
        "1:testing",
        base_url="https://bot.invalid/bot",
        base_file_url="https://bot.invalid/file/bot",
    )
    voice = SimpleNamespace(get_bot=lambda: original)
    try:
        for _ in range(2):
            async with telegram_media.fresh_media_bot(voice) as bot:
                file = await bot.get_file("voice")
                assert bytes(await file.download_as_bytearray()) == b"ogg payload"
            assert transports[-1].closed
        assert transports[0] is not transports[1]
        assert (
            requests
            == ["/bot1:testing/getFile", "/file/bot1:testing/voices/one.ogg"] * 2
        )
        assert not original.request._client.is_closed
    finally:
        await original.request.shutdown()
        await original._request[0].shutdown()
