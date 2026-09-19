"""Standalone relay-server entrypoint; it does not require Telegram config."""

from __future__ import annotations

import asyncio
import os
import ssl

from .node_transport import RelayServer


def _credentials() -> dict[str, str]:
    raw = os.environ.get("CCBOT_RELAY_CREDENTIALS", "")
    result: dict[str, str] = {}
    for item in raw.split(","):
        node_id, separator, secret = item.partition("=")
        if separator and node_id.strip() and secret:
            result[node_id.strip()] = secret
    if not result:
        raise RuntimeError("CCBOT_RELAY_CREDENTIALS is required")
    return result


def _ssl_context() -> ssl.SSLContext | None:
    cert = os.environ.get("CCBOT_RELAY_TLS_CERT", "").strip()
    key = os.environ.get("CCBOT_RELAY_TLS_KEY", "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise RuntimeError("CCBOT_RELAY_TLS_CERT and CCBOT_RELAY_TLS_KEY are both required")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=cert, keyfile=key)
    return context


async def _run() -> None:
    host = os.environ.get("CCBOT_RELAY_HOST", "0.0.0.0")
    port = int(os.environ.get("CCBOT_RELAY_PORT", "8765"))
    leader_id = os.environ.get("CCBOT_NODE_LEADER_ID", "local").strip() or "local"
    server = RelayServer(credentials=_credentials(), leader_id=leader_id)
    await server.start(host, port, ssl=_ssl_context())
    await server.serve_forever()


def main() -> None:
    asyncio.run(_run())


__all__ = ["main"]
