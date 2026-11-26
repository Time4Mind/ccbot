"""Proxy-first Bot API connections with safe, pre-send direct failover."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)
CONNECT_BUDGET = 5.0
PROBE_INTERVAL = 15.0


class ProxyHealth:
    """One recovery probe shared by the outbound and polling connection pools."""

    def __init__(self, probe_transport: Callable[[], httpx.AsyncBaseTransport]) -> None:
        self._probe_transport = probe_transport
        self.recovery: asyncio.Task[None] | None = None

    @property
    def available(self) -> bool:
        return self.recovery is None or self.recovery.done()

    def failed(self, origin: httpx.URL) -> None:
        if self.available:
            logger.warning("Telegram proxy unavailable, using direct connections")
            self.recovery = asyncio.create_task(self._recover(origin))

    async def _recover(self, origin: httpx.URL) -> None:
        # Probe the same HTTPS origin without a bot token or any user payload.
        url = origin.copy_with(path="/", query=None, fragment=None)
        while True:
            await asyncio.sleep(PROBE_INTERVAL)
            try:
                async with (
                    self._probe_transport() as proxy,
                    asyncio.timeout(CONNECT_BUDGET),
                ):
                    response = await proxy.handle_async_request(
                        httpx.Request("HEAD", url)
                    )
                    await response.aclose()
            except (httpx.TransportError, TimeoutError):
                continue
            logger.info("Telegram proxy reachable, returning to proxy")
            return

    async def close(self) -> None:
        if self.recovery is not None:
            self.recovery.cancel()
            await asyncio.gather(self.recovery, return_exceptions=True)


class ProxyFirstTransport(httpx.AsyncBaseTransport):
    def __init__(
        self,
        proxy: httpx.AsyncBaseTransport,
        direct: httpx.AsyncBaseTransport,
        health: ProxyHealth,
    ) -> None:
        self.proxy = proxy
        self.direct = direct
        self.health = health

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not self.health.available:
            return await self.direct.handle_async_request(request)

        original_trace = request.extensions.get("trace")
        sending = False
        deadline = asyncio.timeout(None)

        async def trace(name: str, info: dict[str, Any]) -> None:
            nonlocal sending
            if name.endswith(".connect_tcp.started") and deadline.when() is None:
                deadline.reschedule(asyncio.get_running_loop().time() + CONNECT_BUDGET)
            if name.endswith(".send_request_headers.started"):
                # CONNECT is the HTTP proxy handshake, not the Bot API request.
                if info["request"].method != b"CONNECT":
                    sending = True
                    deadline.reschedule(None)
            if original_trace is not None:
                await original_trace(name, info)

        via_proxy = httpx.Request(
            request.method,
            request.url,
            headers=request.headers,
            stream=request.stream,
            extensions={**request.extensions, "trace": trace},
        )
        try:
            async with deadline:
                return await self.proxy.handle_async_request(via_proxy)
        except TimeoutError:
            if sending or not deadline.expired():
                raise
        except (
            httpx.NetworkError,
            httpx.ProxyError,
            httpx.RemoteProtocolError,
            httpx.TimeoutException,
        ) as exc:
            if sending or isinstance(exc, httpx.PoolTimeout):
                raise
        self.health.failed(request.url)
        return await self.direct.handle_async_request(request)

    async def aclose(self) -> None:
        await self.health.close()
        await asyncio.gather(self.proxy.aclose(), self.direct.aclose())
