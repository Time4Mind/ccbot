"""A fresh, isolated Bot API pool for safe media-read recovery."""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

from telegram import Bot
from telegram.request import HTTPXRequest

from .config import config
from .telegram_proxy_transport import telegram_transport


def media_retry_proxy(attempt: int) -> str | None:
    """Return an explicit route, or None to preserve legacy media recovery."""
    if not config.tg_media_proxy_url:
        return None
    proxy_first = bool(config.tg_proxy_url)
    use_proxy = proxy_first if attempt % 2 == 0 else not proxy_first
    return (config.tg_proxy_url or config.tg_media_proxy_url) if use_proxy else ""


@asynccontextmanager
async def fresh_media_bot(
    voice: Any, *, proxy_url: str | None = None
) -> AsyncGenerator[Bot]:
    from .bot import _app_routes

    original = voice.get_bot()
    transport = telegram_transport(
        pool_size=1,
        proxy_url=config.tg_proxy_url if proxy_url is None else proxy_url,
        socket_options=getattr(_app_routes, "_socket_options")(),
        disable_ipv6=config.tg_disable_ipv6,
    )
    request = HTTPXRequest(
        read_timeout=20.0,
        connect_timeout=10.0,
        write_timeout=10.0,
        pool_timeout=10.0,
        httpx_kwargs={"transport": transport, "trust_env": False},
    )
    async with request:
        # No getMe/polling: only idempotent getFile and file reads use this pool.
        yield Bot(
            original.token,
            base_url=original.base_url.removesuffix(original.token),
            base_file_url=original.base_file_url.removesuffix(original.token),
            local_mode=original.local_mode,
            request=request,
            get_updates_request=request,
        )
