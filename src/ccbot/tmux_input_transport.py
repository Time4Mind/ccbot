"""Bounded literal-input transport for tmux panes."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets

logger = logging.getLogger(__name__)

_CHUNK_BYTES = 900
_CHUNK_PACE = 0.025
_CODEX_LITERAL_INPUT_BARRIER = " "


def terminal_input_chunks(text: str, *, backend: str = "") -> list[bytes]:
    """Return ordered chunks without splitting a UTF-8 code point."""
    transport_text = text
    if backend.strip().lower() == "codex":
        transport_text += _CODEX_LITERAL_INPUT_BARRIER
    payload = transport_text.encode("utf-8")
    if not payload:
        return [b""]
    chunks: list[bytes] = []
    while payload:
        cut = min(len(payload), _CHUNK_BYTES)
        while cut < len(payload) and cut > 0 and payload[cut] & 0xC0 == 0x80:
            cut -= 1
        chunks.append(payload[:cut])
        payload = payload[cut:]
    return chunks


async def _run_tmux(*args: str, input_bytes: bytes | None = None) -> tuple[int, bytes]:
    env = os.environ.copy()
    inherited_tmux = env.pop("TMUX", "")
    env.pop("TMUX_PANE", None)
    command = ["tmux"]
    if inherited_tmux:
        # Keep targeting the inherited server socket without identifying this
        # subprocess as the attached client that launched ccbot.  That client
        # may be read-only (notably Linux control/supervisor deployments), in
        # which case tmux rejects ``send-keys`` with "client is read-only".
        socket_path = inherited_tmux.rsplit(",", 2)[0]
        if socket_path:
            command.extend(("-S", socket_path))
    proc = await asyncio.create_subprocess_exec(
        *command,
        *args,
        stdin=asyncio.subprocess.PIPE if input_bytes is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _stdout, stderr = await proc.communicate(input=input_bytes)
    return proc.returncode or 0, stderr


async def _delete_buffer(buffer_name: str) -> None:
    try:
        await _run_tmux("delete-buffer", "-b", buffer_name)
    except Exception:
        logger.debug("Failed to clean tmux input buffer %s", buffer_name)


async def _paste_chunk(
    window_id: str, buffer_name: str, chunk: bytes
) -> tuple[bool, bool]:
    """Paste one chunk; second result marks an ambiguous paste outcome."""
    loaded = False
    try:
        code, stderr = await _run_tmux(
            "load-buffer", "-b", buffer_name, "-", input_bytes=chunk
        )
        if code:
            logger.error(
                "tmux load-buffer failed window=%s: %s",
                window_id,
                stderr.decode(errors="replace"),
            )
            return False, False
        loaded = True
        try:
            code, stderr = await _run_tmux(
                "paste-buffer", "-d", "-b", buffer_name, "-t", window_id
            )
        except Exception as exc:
            logger.error("tmux paste-buffer ambiguous window=%s: %s", window_id, exc)
            return False, True
        if code:
            logger.error(
                "tmux paste-buffer failed window=%s: %s",
                window_id,
                stderr.decode(errors="replace"),
            )
            return False, False
        loaded = False
        return True, False
    except Exception as exc:
        logger.error("tmux load-buffer failed window=%s: %s", window_id, exc)
        return False, False
    finally:
        if loaded:
            await _delete_buffer(buffer_name)


async def _send_carriage_return(window_id: str) -> bool:
    try:
        code, stderr = await _run_tmux("send-keys", "-t", window_id, "C-m")
    except Exception as exc:
        logger.error("tmux C-m failed window=%s: %s", window_id, exc)
        return False
    if code:
        logger.error(
            "tmux C-m failed window=%s: %s",
            window_id,
            stderr.decode(errors="replace"),
        )
        return False
    return True


async def send_special_key(window_id: str, key: str, *, enter: bool = False) -> bool:
    """Send named tmux keys through an independent writable command client."""
    keys = [key] if key else []
    if enter:
        keys.append("C-m")
    if not keys:
        return True
    try:
        code, stderr = await _run_tmux("send-keys", "-t", window_id, *keys)
    except Exception as exc:
        logger.error("tmux special key failed window=%s: %s", window_id, exc)
        return False
    if code:
        logger.error(
            "tmux special key failed window=%s: %s",
            window_id,
            stderr.decode(errors="replace"),
        )
        return False
    return True


async def paste_literal(window_id: str, text: str) -> bool:
    """Paste literal text without Enter through writable command clients."""
    if not text:
        return True
    operation = f"ccbot-{secrets.token_hex(8)}"
    for index, chunk in enumerate(terminal_input_chunks(text)):
        name = operation if index == 0 else f"{operation}-{index}"
        ok, _ambiguous = await _paste_chunk(window_id, name, chunk)
        if not ok:
            return False
        await asyncio.sleep(_CHUNK_PACE)
    return True


async def send_literal_chunked(window_id: str, text: str, *, backend: str = "") -> bool:
    chunks = terminal_input_chunks(text, backend=backend)
    operation = f"ccbot-{secrets.token_hex(8)}"
    pasted = 0
    for index, chunk in enumerate(chunks):
        name = operation if index == 0 else f"{operation}-{index}"
        ok, ambiguous = await _paste_chunk(window_id, name, chunk)
        if not ok:
            if pasted or ambiguous:
                if backend.strip().lower() == "codex" and not chunk.endswith(b" "):
                    await _paste_chunk(window_id, f"{operation}-barrier", b" ")
                await _send_carriage_return(window_id)
            return False
        pasted += 1
        await asyncio.sleep(_CHUNK_PACE)
    return await _send_carriage_return(window_id)
