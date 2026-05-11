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
import sys
import time

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

    Always installs a stderr handler. Additionally writes a rotating
    untruncated log to ``$CCBOT_DIR/logs/bot.log`` (default
    ``~/.ccbot/logs/bot.log``) so debugging the bot doesn't depend on
    grepping ``tmux capture-pane`` output, which is wrapped to the
    pane's column width and drops the rest of each line.
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
    root.addHandler(stderr_handler)

    # File handler: untruncated lines, rotated daily, last 7 days kept.
    # Use ``ccbot_dir`` so the path follows ``CCBOT_DIR`` env var (and
    # therefore matches wherever ``state.json`` lives).
    try:
        from logging.handlers import TimedRotatingFileHandler

        from .utils import ccbot_dir

        logs_dir = ccbot_dir() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            logs_dir / "bot.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except Exception as e:
        # File logging is best-effort — never block the bot if the path
        # is unwritable or the rotator can't lock.
        root.warning("File logging unavailable: %s", e)
