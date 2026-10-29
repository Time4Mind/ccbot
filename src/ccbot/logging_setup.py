"""Logging configuration — human-readable by default, JSON when asked.

Activated by ``main.py`` at process start. The JSON formatter is opt-in
via ``CCBOT_LOG_FORMAT=json`` (or ``=structured``); any other value
keeps the default human-readable single-line layout. JSON mode emits
every ``extra={}`` key the call site passed, so downstream tooling
(``jq``, log aggregators) can index events by feature without parsing
free-form text.

Public API:
  configure_logging() -> None
      Idempotent. Replaces the root logger's handlers with one based on
      the resolved formatter; respects ``LOG_LEVEL`` env var (default
      INFO).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

# Standard LogRecord attributes — anything else on the record is an
# ``extra=`` payload contributed by the call site, and we surface those
# in the JSON output verbatim.
_STANDARD_LOGRECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "asctime",
        "message",
    }
)

_LOG_RETENTION_SECONDS = 6 * 3600
_ROTATED_BOT_LOG = re.compile(r"^bot\.log\.\d{4}-\d{2}-\d{2}(?:_\d{2})?$")


def _prune_expired_logs(logs_dir: Path) -> None:
    """Split legacy active logs into hours and prune without buffering them."""
    now = time.time()
    cutoff = now - _LOG_RETENTION_SECONDS
    cutoff_text = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cutoff))
    current_hour = time.strftime("%Y-%m-%d %H", time.localtime(now))
    for path in logs_dir.iterdir():
        if _ROTATED_BOT_LOG.fullmatch(path.name) and path.stat().st_mtime < cutoff:
            path.unlink()
    current = logs_dir / "bot.log"
    if not current.is_file():
        return
    mode = current.stat().st_mode & 0o777
    temporaries: dict[Path, str] = {}
    outputs: dict[Path, TextIO] = {}
    keep = True
    target = current
    try:
        with ExitStack() as stack:
            source = stack.enter_context(
                current.open("r", encoding="utf-8", errors="replace")
            )
            for line in source:
                stamp = line[:19]
                if line.startswith('{"ts":'):
                    match = re.match(
                        r'^\{"ts":\s*"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)', line
                    )
                    stamp = match.group(1).replace("T", " ") if match else ""
                if (
                    len(stamp) == 19
                    and stamp[4] == "-"
                    and stamp[7] == "-"
                    and stamp[10] == " "
                    and stamp[13] == ":"
                    and stamp[16] == ":"
                ):
                    keep = stamp >= cutoff_text
                    target = (
                        current
                        if stamp[:13] >= current_hour
                        else logs_dir / f"bot.log.{stamp[:10]}_{stamp[11:13]}"
                    )
                if keep:
                    if target not in outputs:
                        fd, name = tempfile.mkstemp(
                            prefix=".bot-log-prune-", dir=logs_dir
                        )
                        os.fchmod(fd, mode)
                        temporaries[target] = name
                        outputs[target] = stack.enter_context(
                            os.fdopen(fd, "w", encoding="utf-8")
                        )
                    outputs[target].write(line)
            for stream in outputs.values():
                stream.flush()
                os.fsync(stream.fileno())
        # A previous partial migration must not silently duplicate records.
        if any(path != current and path.exists() for path in temporaries):
            raise OSError("hourly log segment already exists")
        if current not in temporaries:
            fd, name = tempfile.mkstemp(prefix=".bot-log-prune-", dir=logs_dir)
            os.fchmod(fd, mode)
            os.close(fd)
            temporaries[current] = name
        for path, name in temporaries.items():
            if path != current:
                os.replace(name, path)
        os.replace(temporaries[current], current)
    finally:
        for name in temporaries.values():
            if os.path.exists(name):
                os.unlink(name)


class JsonFormatter(logging.Formatter):
    """Single-line JSON per record, with ``extra={}`` keys hoisted to the top."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created))
            + f".{int(record.msecs):03d}",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _STANDARD_LOGRECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _resolve_format() -> str:
    """``CCBOT_LOG_FORMAT`` env → ``"json"`` or ``"human"``."""
    raw = os.environ.get("CCBOT_LOG_FORMAT", "").strip().lower()
    if raw in ("json", "structured"):
        return "json"
    return "human"


def configure_logging() -> None:
    """Wire up the root logger. Safe to call multiple times — replaces handlers.

    Writes one rotating, untruncated log to ``$CCBOT_DIR/logs/bot.log`` (default
    ``~/.ccbot/logs/bot.log``) so debugging the bot doesn't depend on
    grepping ``tmux capture-pane`` output, which is wrapped to the
    pane's column width and drops the rest of each line. Falls back to stderr
    only when the log file cannot be opened.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    fmt = (
        JsonFormatter()
        if _resolve_format() == "json"
        else logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    # Replace any handlers that python-telegram-bot or our previous import
    # paths may have already attached — keep one consistent stream.
    for h in list(root.handlers):
        root.removeHandler(h)
    # The current hour plus five closed hours retains at most six hourly
    # segments. Rotation happens on writes and costs nothing while idle.
    # Use ``ccbot_dir`` so the path follows ``CCBOT_DIR`` env var (and
    # therefore matches wherever ``state.json`` lives).
    try:
        from logging.handlers import TimedRotatingFileHandler

        from .utils import ccbot_dir

        logs_dir = ccbot_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        prune_error = None
        try:
            _prune_expired_logs(logs_dir)
        except OSError as exc:
            prune_error = type(exc).__name__
        file_handler = TimedRotatingFileHandler(
            logs_dir / "bot.log",
            when="H",
            interval=1,
            backupCount=5,
            encoding="utf-8",
        )
        current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
        file_handler.rolloverAt = int((current_hour + timedelta(hours=1)).timestamp())
        for expired in file_handler.getFilesToDelete():
            os.unlink(expired)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
        if prune_error:
            root.warning("Log retention cleanup failed error_type=%s", prune_error)
    except Exception as e:
        # Keep one working destination when the file is unavailable.
        root.addHandler(stderr_handler)
        root.warning("File logging unavailable: %s", e)
