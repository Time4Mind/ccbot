"""``ccbot send-file`` — on-demand outbound delivery for a running session.

A session hands the user a file (image, document, archive, report, etc.)
by calling this directly — ``ccbot send-file <path>`` — instead of relying
on a background poller. No special drop directory, no delay: it sends
immediately and prints a pass/fail line per target chat, so the invoking
Bash tool call carries real feedback back to Claude.

Target chat resolution, in order:
  1. ``--chat-id ID`` — explicit override.
  2. ``$CCBOT_CHAT_ID`` — set by ``tmux_manager`` when the session was
     spawned (the user who created/owns it). The common case.
  3. Neither set — broadcast to every ``ALLOWED_USERS`` entry (matches
     the maintenance windows that have no single owning chat).

Usage: ``ccbot send-file <path> [--caption TEXT] [--chat-id ID]``
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys
from pathlib import Path
from typing import Any

_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def resolve_chat_ids(
    cli_chat_id: int | None, env_chat_id: str | None, allowed_users: set[int]
) -> list[int]:
    """Pick the send target(s) per the precedence documented in the module docstring."""
    if cli_chat_id is not None:
        return [cli_chat_id]
    if env_chat_id:
        return [int(env_chat_id)]
    return sorted(allowed_users)


def send_file_main() -> None:
    parser = argparse.ArgumentParser(prog="ccbot send-file")
    parser.add_argument("path")
    parser.add_argument("--caption", default=None)
    parser.add_argument("--chat-id", type=int, default=None)
    args = parser.parse_args(sys.argv[2:])

    path = Path(args.path).expanduser()
    if not path.is_file():
        print(f"Error: not a file: {path}", file=sys.stderr)
        sys.exit(1)

    from .config import config

    chat_ids = resolve_chat_ids(
        args.chat_id, os.getenv("CCBOT_CHAT_ID"), config.allowed_users
    )
    if not chat_ids:
        print(
            "Error: no target chat — set CCBOT_CHAT_ID or pass --chat-id",
            file=sys.stderr,
        )
        sys.exit(1)

    ok = asyncio.run(_send_all(path, args.caption, chat_ids))
    sys.exit(0 if ok else 1)


async def deliver(
    bot: Any, path: Path, caption: str | None, chat_ids: list[int]
) -> bool:
    """Send ``path`` to every chat in ``chat_ids``; print a result line each.

    Returns True iff every send succeeded. ``bot`` must already be usable
    (initialized) — callers own its lifecycle.
    """
    from telegram.error import TelegramError

    is_photo = path.suffix.lower() in _PHOTO_EXTS
    kind = "photo" if is_photo else "document"
    data = path.read_bytes()
    ok = True
    for chat_id in chat_ids:
        try:
            if is_photo:
                await bot.send_photo(
                    chat_id=chat_id, photo=io.BytesIO(data), caption=caption
                )
            else:
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(data),
                    filename=path.name,
                    caption=caption,
                )
            print(f"sent {path.name} ({kind}) -> {chat_id}: ok")
        except TelegramError as e:
            ok = False
            print(f"sent {path.name} -> {chat_id}: FAILED ({e})", file=sys.stderr)
    return ok


async def _send_all(path: Path, caption: str | None, chat_ids: list[int]) -> bool:
    from telegram import Bot

    from .config import config

    request = None
    if config.tg_proxy_url:
        from telegram.request import HTTPXRequest

        request = HTTPXRequest(proxy=config.tg_proxy_url)

    bot = Bot(token=config.telegram_bot_token, request=request)
    async with bot:
        return await deliver(bot, path, caption, chat_ids)
