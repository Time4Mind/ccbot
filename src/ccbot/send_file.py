"""``ccbot send-file`` — on-demand outbound delivery for a running session.

A session hands the user a file (image, document, archive, report, etc.)
by calling this directly — ``ccbot send-file <path>``. The CLI relays the
request over a local Unix socket to the already-running ccbot daemon, which
owns the initialized Telegram client and has host network access. This keeps
delivery working when the agent itself runs in a network-restricted sandbox.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

_PHOTO_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
_IPC_TIMEOUT_SECONDS = 180.0
_MAX_IPC_LINE_BYTES = 64 * 1024

logger = logging.getLogger(__name__)

Reporter = Callable[[str, bool], None]


class SendFileDaemonUnavailable(ConnectionError):
    """The local ccbot daemon socket could not be reached."""


def send_file_socket_path() -> Path:
    """Return a sandbox-reachable, per-deployment Unix socket path.

    Managed Codex profiles allow local Unix sockets but enforce filesystem
    access on the socket path. ``$CCBOT_DIR`` normally lives under the home
    directory and is read-only to a session, so connecting to a socket there
    fails with ``EPERM``. ``/tmp`` is an explicit writable sandbox root. The
    config-dir digest keeps production and staging sockets separate for the
    same Unix user.
    """
    from .utils import ccbot_dir

    deployment = str(ccbot_dir().expanduser().resolve())
    digest = hashlib.sha256(deployment.encode("utf-8")).hexdigest()[:12]
    return Path("/tmp") / f"ccbot-send-file-{os.getuid()}-{digest}.sock"


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
    socket_path: Path | None = None,
) -> bool:
    """Ask the running bot daemon to send a file and relay its result lines."""
    target = socket_path or send_file_socket_path()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(target)), timeout=3.0
        )
    except (
        FileNotFoundError,
        ConnectionRefusedError,
        OSError,
        asyncio.TimeoutError,
    ) as e:
        raise SendFileDaemonUnavailable(str(e)) from e

    request = {
        "path": str(path.resolve()),
        "caption": caption,
        "chat_ids": chat_ids,
    }
    try:
        writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=_IPC_TIMEOUT_SECONDS)
        if not raw:
            raise RuntimeError("ccbot daemon closed the connection without a result")
        response = json.loads(raw)
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
        writer.close()
        await writer.wait_closed()


async def _handle_send_file_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    bot: Any,
    allowed_users: set[int],
) -> None:
    """Serve one local send-file request from an agent session."""
    response: dict[str, Any]
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=10.0)
        if not raw:
            raise ValueError("empty request")
        if len(raw) > _MAX_IPC_LINE_BYTES:
            raise ValueError("request is too large")
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("request must be a JSON object")

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
        logger.exception("Local send-file request failed: %s", e)
        response = {"ok": False, "error": str(e), "reports": []}

    try:
        writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
    except (ConnectionError, OSError):
        logger.debug("send-file client disconnected before receiving the result")
    finally:
        writer.close()
        await writer.wait_closed()


async def start_send_file_server(
    bot: Any,
    *,
    socket_path: Path | None = None,
    allowed_users: set[int] | None = None,
) -> asyncio.AbstractServer:
    """Start the daemon-side Unix socket and return its asyncio server."""
    from functools import partial

    from .config import config

    target = socket_path or send_file_socket_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.unlink()
    except FileNotFoundError:
        pass

    server = await asyncio.start_unix_server(
        partial(
            _handle_send_file_client,
            bot=bot,
            allowed_users=set(allowed_users or config.allowed_users),
        ),
        path=str(target),
        limit=_MAX_IPC_LINE_BYTES,
    )
    target.chmod(0o600)
    logger.info("send-file IPC server listening on %s", target)
    return server


async def stop_send_file_server(
    server: asyncio.AbstractServer, socket_path: Path | None = None
) -> None:
    """Stop the daemon-side server and remove its socket file."""
    target = socket_path or send_file_socket_path()
    server.close()
    await server.wait_closed()
    try:
        target.unlink()
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
