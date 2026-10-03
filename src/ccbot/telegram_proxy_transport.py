"""Close failed CONNECT tunnels before httpcore releases their connect lock.

httpcore 1.0.9 leaves a CONNECT connection ACTIVE if proxy start_tls fails.
The documented trace hook runs inside that connection's connect lock, so it
can close only the failed tunnel without touching concurrent requests. Keep
the private factory compatibility shim here; regression tests exercise it
through HTTPXRequest and the real Bot API/rich-edit cancellation path.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpcore
import httpx
from httpcore._async.http_proxy import AsyncTunnelHTTPConnection

# The per-connection factory is private in the supported httpcore 1.0 line.
# pyright: reportPrivateUsage=false


class _ClosingTunnel(AsyncTunnelHTTPConnection):
    async def handle_async_request(
        self, request: httpcore.Request
    ) -> httpcore.Response:
        original_trace = request.extensions.get("trace")

        async def trace(event: str, info: dict[str, Any]) -> None:
            if event == "proxy.start_tls.failed":
                # Stay inside the connect lock until cleanup completes, even
                # if a caller cancels again while aclose is yielding.
                cleanup = asyncio.create_task(self.aclose())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                cleanup.result()
            if original_trace is not None:
                await original_trace(event, info)

        guarded = httpcore.Request(
            method=request.method,
            url=request.url,
            headers=request.headers,
            content=request.stream,
            extensions={**request.extensions, "trace": trace},
        )
        return await super().handle_async_request(guarded)


class _ClosingProxy(httpcore.AsyncHTTPProxy):
    def create_connection(
        self, origin: httpcore.Origin
    ) -> httpcore.AsyncConnectionInterface:
        if origin.scheme == b"http":
            return super().create_connection(origin)
        return _ClosingTunnel(
            proxy_origin=self._proxy_url.origin,
            proxy_headers=self._proxy_headers,
            remote_origin=origin,
            ssl_context=self._ssl_context,
            proxy_ssl_context=self._proxy_ssl_context,
            keepalive_expiry=self._keepalive_expiry,
            http1=self._http1,
            http2=self._http2,
            network_backend=self._network_backend,
            socket_options=self._socket_options,
        )


def telegram_transport(
    *, pool_size: int, proxy_url: str, socket_options: list[tuple[int, int, int]]
) -> httpx.AsyncHTTPTransport:
    limits = httpx.Limits(
        max_connections=pool_size, max_keepalive_connections=pool_size
    )
    transport = httpx.AsyncHTTPTransport(
        limits=limits, proxy=proxy_url or None, socket_options=socket_options
    )
    if proxy_url and isinstance(transport._pool, httpcore.AsyncHTTPProxy):
        proxy = httpx.Proxy(proxy_url)
        transport._pool = _ClosingProxy(
            proxy_url=str(proxy.url),
            proxy_auth=proxy.raw_auth,
            proxy_headers=proxy.headers.raw,
            proxy_ssl_context=proxy.ssl_context,
            ssl_context=transport._pool._ssl_context,
            max_connections=pool_size,
            max_keepalive_connections=pool_size,
            keepalive_expiry=limits.keepalive_expiry,
            socket_options=socket_options,
        )
    return transport
