"""Opt-in, bounded voice reads through the same bot's MTProto authorization.

This is a file download only: no update polling, user-account login, transcription
or prompt dispatch. The Bot API remains the primary transport. A temporary bot
session lives in RAM and is logged out after each recovery attempt.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import logging
from importlib import import_module
import os
import struct
from typing import Any
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)
_TIMEOUT = 25.0
_MAX_BYTES = 20 * 1024 * 1024
_lock = asyncio.Lock()


def is_enabled() -> bool:
    return os.getenv("TG_MEDIA_MTPROTO_FALLBACK", "").lower() in ("1", "true", "yes")


def document_location(file_id: str) -> tuple[int, int, int, bytes]:
    """Decode the document identity/reference in a Telegram v4 voice file_id.

    Telegram's zero-run/base64 encoding and TL byte-string layout are parsed
    strictly. Unknown versions/types fail closed and leave Bot API retries intact.
    """
    if not 1 <= len(file_id) <= 4096:
        raise ValueError("Unsupported voice identifier")
    encoded = base64.b64decode(
        file_id + "=" * (-len(file_id) % 4), altchars=b"-_", validate=True
    )
    raw = bytearray()
    index = 0
    while index < len(encoded):
        value = encoded[index]
        index += 1
        if value:
            raw.append(value)
        else:
            if index == len(encoded) or not encoded[index]:
                raise ValueError("Invalid voice identifier run")
            raw.extend(b"\0" * encoded[index])
            index += 1
        if len(raw) > 8192:
            raise ValueError("Oversized voice identifier")
    if len(raw) < 26 or raw[-1] != 4:
        raise ValueError("Unsupported voice identifier version")
    kind, dc = struct.unpack_from("<ii", raw)
    if kind not in (3, 3 | (1 << 25)) or not 1 <= dc <= 5:
        raise ValueError("Unsupported voice document")
    offset, reference = 8, b""
    if kind & (1 << 25):
        size = raw[offset]
        prefix = 1
        if size == 254:
            size = int.from_bytes(raw[offset + 1 : offset + 4], "little")
            prefix = 4
        elif size == 255:
            raise ValueError("Invalid file reference")
        end = offset + prefix + size
        if end > len(raw) - 18:
            raise ValueError("Truncated file reference")
        reference = bytes(raw[offset + prefix : end])
        offset += (prefix + size + 3) // 4 * 4
    if offset + 16 > len(raw) - 2:
        raise ValueError("Truncated voice document")
    document_id, access_hash = struct.unpack_from("<qq", raw, offset)
    return dc, document_id, access_hash, reference


def _client(api_id: int, api_hash: str, proxy_url: str) -> Any:
    # Optional dependency: ordinary installations keep the previous path.
    TelegramClient = import_module("telethon").TelegramClient
    MemorySession = import_module("telethon.sessions").MemorySession

    proxy = None
    if proxy_url:
        url = urlsplit(proxy_url)
        kind = url.scheme.removesuffix("h")
        if kind not in ("http", "socks4", "socks5") or not url.hostname or not url.port:
            raise ValueError("Unsupported media proxy")
        proxy = {
            "proxy_type": kind,
            "addr": url.hostname,
            "port": url.port,
            "username": unquote(url.username) if url.username else None,
            "password": unquote(url.password) if url.password else None,
        }
    return TelegramClient(
        MemorySession(),
        api_id,
        api_hash,
        receive_updates=False,
        connection_retries=0,
        request_retries=0,
        timeout=10,
        flood_sleep_threshold=0,
        proxy=proxy,
    )


def _location(document_id: int, access_hash: int, reference: bytes) -> Any:
    factory = import_module("telethon.tl.types").InputDocumentFileLocation
    return factory(document_id, access_hash, reference, "")


async def _read(voice: Any, expected_size: int, proxy_url: str | None = None) -> bytes:
    from .config import config

    dc, document_id, access_hash, reference = document_location(voice.file_id)
    client = _client(
        int(config.tg_api_id),
        config.tg_api_hash,
        config.tg_proxy_url if proxy_url is None else proxy_url,
    )
    token = voice.get_bot().token
    try:
        await client.connect()
        await client.sign_in(bot_token=token)
        identity = await client.get_me()
        if not identity.bot or identity.id != int(token.split(":", 1)[0]):
            raise ValueError("Media bot identity mismatch")
        location = _location(document_id, access_hash, reference)
        # Bound the output before accumulating it; a claimed size is not a limit.
        chunks = bytearray()
        async for part in client.iter_download(
            location, dc_id=dc, file_size=expected_size
        ):
            if len(chunks) + len(part) > expected_size:
                raise ValueError("Voice exceeds declared size")
            chunks.extend(part)
        if len(chunks) != expected_size or not chunks.startswith(b"OggS"):
            raise ValueError("Downloaded voice does not match its metadata")
        return bytes(chunks)
    finally:
        try:
            async with asyncio.timeout(5):
                await client.log_out()
        except Exception as exc:
            logger.warning(
                "voice_mtproto_logout_failed error_type=%s", type(exc).__name__
            )
        finally:
            with suppress(Exception):
                async with asyncio.timeout(5):
                    await client.disconnect()


async def download_voice(voice: Any, *, proxy_url: str | None = None) -> bytes | None:
    """Return verified bytes or leave the original bounded retry path in charge."""
    if not is_enabled():
        return None
    try:
        size = voice.file_size
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or not 0 < size <= _MAX_BYTES
        ):
            raise ValueError("Missing or excessive voice size")
        async with asyncio.timeout(_TIMEOUT):
            async with _lock:
                audio = await _read(voice, size, proxy_url)
        logger.info("voice_mtproto_download_ok bytes=%s", len(audio))
        return audio
    except Exception as exc:
        # Never log credential-bearing URLs, file references or exception text.
        logger.warning(
            "voice_mtproto_download_failed error_type=%s", type(exc).__name__
        )
        return None
