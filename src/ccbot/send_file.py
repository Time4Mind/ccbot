"""``ccbot send-file`` — on-demand outbound delivery for a running session.

A session hands the user a file (image, document, archive, report, etc.)
by calling this directly — ``ccbot send-file <path>``. The CLI writes a small
request into a per-deployment relay directory under ``/tmp``; the already-
running ccbot daemon picks it up, uses its initialized Telegram client, and
writes the real delivery result back. A filesystem relay works inside managed
agent sandboxes that block both external network and cross-process sockets.

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
import hashlib
import io
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_RELAY_TIMEOUT_SECONDS = 180.0
_RELAY_POLL_SECONDS = 0.1
_RELAY_READY_MAX_AGE_SECONDS = 5.0
_RELAY_CLEANUP_AGE_SECONDS = 3600.0

logger = logging.getLogger(__name__)

Reporter = Callable[[str, bool], None]


class SendFileDaemonUnavailable(ConnectionError):
    """The local ccbot daemon relay is absent or stale."""


def send_file_relay_dir() -> Path:
    """Return the sandbox-writable, per-deployment relay directory.

    ``/tmp`` is an explicit writable root in managed Codex profiles. The
    config-dir digest keeps production and staging queues separate for the
    same Unix user without exposing their config paths in request filenames.
    """
    from .utils import ccbot_dir

    deployment = str(ccbot_dir().expanduser().resolve())
    digest = hashlib.sha256(deployment.encode("utf-8")).hexdigest()[:12]
    return Path("/tmp") / f"ccbot-send-file-{os.getuid()}-{digest}"


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

    try:
        ok = asyncio.run(_send_via_daemon(path, args.caption, chat_ids))
    except SendFileDaemonUnavailable as e:
        # Keep the command useful for maintenance shells where the bot daemon
        # is intentionally stopped. In a managed Codex sandbox this fallback
        # will fail cleanly, without the former multi-page traceback.
        print(
            f"ccbot daemon unavailable ({e}); trying direct delivery", file=sys.stderr
        )
        try:
            ok = asyncio.run(_send_all(path, args.caption, chat_ids))
        except Exception as direct_error:
            print(f"send-file FAILED ({direct_error})", file=sys.stderr)
            ok = False
    except Exception as e:
        print(f"send-file FAILED ({e})", file=sys.stderr)
        ok = False
    sys.exit(0 if ok else 1)


async def deliver(
    bot: Any,
    path: Path,
    caption: str | None,
    chat_ids: list[int],
    reporter: Reporter | None = None,
) -> bool:
    """Send ``path`` to every chat in ``chat_ids``; print a result line each.

    Returns True iff every send succeeded. ``bot`` must already be usable
    (initialized) — callers own its lifecycle.
    """
    from telegram.error import TelegramError

    def report(message: str, is_error: bool = False) -> None:
        if reporter is not None:
            reporter(message, is_error)
        else:
            print(message, file=sys.stderr if is_error else sys.stdout)

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
            report(f"sent {path.name} ({kind}) -> {chat_id}: ok")
        except TelegramError as e:
            ok = False
            report(f"sent {path.name} -> {chat_id}: FAILED ({e})", True)
    return ok


async def _send_via_daemon(
    path: Path,
    caption: str | None,
    chat_ids: list[int],
    relay_dir: Path | None = None,
) -> bool:
    """Queue a file for the running daemon and relay its result lines."""
    from .utils import atomic_write_json

    target = relay_dir or send_file_relay_dir()
    ready_path = target / ".ready"
    try:
        ready_age = time.time() - ready_path.stat().st_mtime
    except OSError as e:
        raise SendFileDaemonUnavailable("relay is not running") from e
    if ready_age > _RELAY_READY_MAX_AGE_SECONDS:
        raise SendFileDaemonUnavailable(f"relay heartbeat is stale ({ready_age:.1f}s)")

    request_id = uuid.uuid4().hex
    request_path = target / f"{request_id}.request.json"
    response_path = target / f"{request_id}.response.json"
    request = {
        "request_id": request_id,
        "path": str(path.resolve()),
        "caption": caption,
        "chat_ids": chat_ids,
    }
    try:
        atomic_write_json(request_path, request)
        deadline = asyncio.get_running_loop().time() + _RELAY_TIMEOUT_SECONDS
        while not response_path.is_file():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("ccbot daemon did not return a delivery result")
            await asyncio.sleep(min(_RELAY_POLL_SECONDS, remaining))

        response = json.loads(response_path.read_text(encoding="utf-8"))
        if not isinstance(response, dict):
            raise RuntimeError("invalid response from ccbot daemon")
        reports = response.get("reports", [])
        if isinstance(reports, list):
            for report in reports:
                if not isinstance(report, dict):
                    continue
                message = report.get("message")
                if isinstance(message, str):
                    print(
                        message,
                        file=sys.stderr if report.get("error") else sys.stdout,
                    )
        if not response.get("ok") and response.get("error"):
            print(f"send-file FAILED ({response['error']})", file=sys.stderr)
        return bool(response.get("ok"))
    finally:
        for cleanup_path in (request_path, response_path):
            try:
                cleanup_path.unlink()
            except FileNotFoundError:
                pass


async def _process_relay_request(
    request_path: Path,
    *,
    bot: Any,
    allowed_users: set[int],
) -> None:
    """Claim and process one filesystem-relay request."""
    from .utils import atomic_write_json

    request_id = request_path.name.removesuffix(".request.json")
    processing_path = request_path.with_name(f"{request_id}.processing.json")
    response_path = request_path.with_name(f"{request_id}.response.json")
    response: dict[str, Any]
    try:
        request_path.rename(processing_path)
    except FileNotFoundError:
        return
    except OSError as e:
        logger.warning("Could not claim send-file request %s: %s", request_path, e)
        return

    try:
        request = json.loads(processing_path.read_text(encoding="utf-8"))
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")
        if request.get("request_id") != request_id:
            raise ValueError("request_id does not match request filename")

        raw_path = request.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("path is required")
        path = Path(raw_path)
        if not path.is_file():
            raise ValueError(f"not a file: {path}")

        raw_chat_ids = request.get("chat_ids")
        if not isinstance(raw_chat_ids, list) or not raw_chat_ids:
            raise ValueError("at least one chat_id is required")
        if not all(isinstance(chat_id, int) for chat_id in raw_chat_ids):
            raise ValueError("chat_ids must be integers")
        chat_ids = list(dict.fromkeys(raw_chat_ids))
        denied = sorted(set(chat_ids) - allowed_users)
        if denied:
            raise PermissionError(f"chat_id is not in ALLOWED_USERS: {denied}")

        caption = request.get("caption")
        if caption is not None and not isinstance(caption, str):
            raise ValueError("caption must be a string")

        reports: list[dict[str, Any]] = []

        def collect_report(message: str, is_error: bool) -> None:
            reports.append({"message": message, "error": is_error})

        ok = await deliver(bot, path, caption, chat_ids, reporter=collect_report)
        response = {"ok": ok, "reports": reports}
    except Exception as e:
        logger.exception("Filesystem send-file request failed: %s", e)
        response = {"ok": False, "error": str(e), "reports": []}

    try:
        atomic_write_json(response_path, response)
    except OSError as e:
        logger.error("Could not write send-file response %s: %s", response_path, e)
    finally:
        try:
            processing_path.unlink()
        except FileNotFoundError:
            pass


async def send_file_relay_loop(
    bot: Any,
    *,
    relay_dir: Path | None = None,
    allowed_users: set[int] | None = None,
) -> None:
    """Continuously process sandbox-authored send-file request files."""
    from .config import config

    target = relay_dir or send_file_relay_dir()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    target.chmod(0o700)
    ready_path = target / ".ready"
    effective_allowed_users = set(allowed_users or config.allowed_users)

    # Recover requests claimed by a daemon that stopped before producing a
    # response. This can duplicate a Telegram send only in the tiny crash
    # window after Telegram accepted it but before the response file landed.
    for processing_path in target.glob("*.processing.json"):
        request_id = processing_path.name.removesuffix(".processing.json")
        request_path = target / f"{request_id}.request.json"
        if not request_path.exists():
            processing_path.rename(request_path)

    logger.info("send-file filesystem relay watching %s", target)
    pending_tasks: set[asyncio.Task[None]] = set()
    next_heartbeat = 0.0
    next_cleanup = 0.0
    try:
        while True:
            now = asyncio.get_running_loop().time()
            if now >= next_heartbeat:
                ready_path.touch()
                next_heartbeat = now + 1.0
            for request_path in sorted(target.glob("*.request.json")):
                task = asyncio.create_task(
                    _process_relay_request(
                        request_path,
                        bot=bot,
                        allowed_users=effective_allowed_users,
                    )
                )
                pending_tasks.add(task)
                task.add_done_callback(pending_tasks.discard)

            if now >= next_cleanup:
                cutoff = time.time() - _RELAY_CLEANUP_AGE_SECONDS
                for stale_path in target.glob("*.response.json"):
                    try:
                        if stale_path.stat().st_mtime < cutoff:
                            stale_path.unlink()
                    except FileNotFoundError:
                        pass
                next_cleanup = now + 60.0
            await asyncio.sleep(_RELAY_POLL_SECONDS)
    finally:
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        try:
            ready_path.unlink()
        except FileNotFoundError:
            pass


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
