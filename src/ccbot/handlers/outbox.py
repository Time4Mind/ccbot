"""Outbound file delivery — Claude drops a file, the bot ships it out.

Spec section 7 (Files and media), outbound half — symmetric to
``inbox.py``'s inbound half:

  - A session hands the user a file by placing it at
    ``<workdir>/.ccbot-outbox/<filename>``. Convention: write it elsewhere
    first, then ``mv`` it into that directory — rename is atomic, so a
    sweep never observes a half-written file.
  - ``outbox_sweep`` polls every known session's outbox dir, delivers each
    settled file to every allowed user (sessions aren't per-user
    partitioned — see ``session.py``), then deletes it on success. Images
    go out via ``send_photo``; everything else via ``send_document``.

Public API:
  ccbot_outbox_dir(workdir) -> Path
  outbox_sweep(bot) -> int
"""

from __future__ import annotations

import io
import logging
import time
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from ..config import config
from ..session import session_manager

logger = logging.getLogger(__name__)

_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# Skip files younger than this — guards against a sweep racing a writer
# that hasn't finished its ``mv`` into the outbox dir yet.
_MIN_AGE_SECONDS = 1.0


def ccbot_outbox_dir(workdir: str) -> Path:
    """Return (and ensure) the .ccbot-outbox directory for a session's workdir."""
    outbox = Path(workdir).expanduser().resolve() / config.outbox_dirname
    outbox.mkdir(parents=True, exist_ok=True)
    return outbox


async def outbox_sweep(bot: Bot) -> int:
    """Deliver and remove every settled file waiting in a session's outbox.

    Returns the number of files delivered (counted once per file, even
    though each one goes out to every allowed user).
    """
    now = time.time()
    seen: set[Path] = set()
    sent = 0
    for sess in session_manager.sessions.values():
        if not sess.workdir:
            continue
        try:
            outbox = Path(sess.workdir).expanduser().resolve() / config.outbox_dirname
        except (OSError, ValueError):
            continue
        if outbox in seen or not outbox.is_dir():
            continue
        seen.add(outbox)
        for entry in sorted(outbox.iterdir()):
            try:
                if not entry.is_file():
                    continue
                if now - entry.stat().st_mtime < _MIN_AGE_SECONDS:
                    continue
            except OSError as e:
                logger.debug("outbox_sweep stat skip %s: %s", entry, e)
                continue
            if await _deliver(bot, entry):
                sent += 1
    return sent


async def _deliver(bot: Bot, path: Path) -> bool:
    """Send one file to every allowed user; unlink it iff all sends land."""
    try:
        data = path.read_bytes()
    except OSError as e:
        logger.debug("outbox_sweep read skip %s: %s", path, e)
        return False
    is_photo = path.suffix.lower() in _PHOTO_EXTS
    ok = True
    for user_id in sorted(config.allowed_users):
        try:
            if is_photo:
                await bot.send_photo(chat_id=user_id, photo=io.BytesIO(data))
            else:
                await bot.send_document(
                    chat_id=user_id,
                    document=io.BytesIO(data),
                    filename=path.name,
                )
        except TelegramError as e:
            ok = False
            logger.warning("outbox deliver %s -> %s failed: %s", path, user_id, e)
    if ok:
        try:
            path.unlink()
        except OSError as e:
            logger.debug("outbox_sweep unlink failed %s: %s", path, e)
    return ok
