"""Tests for logging_setup — JsonFormatter shape + extras hoisting."""

import json
import logging
import sys
from io import StringIO
from logging.handlers import TimedRotatingFileHandler

from ccbot.logging_setup import JsonFormatter, configure_logging


def _record(
    msg: str = "hello",
    name: str = "ccbot.test",
    level: int = logging.INFO,
    extras: dict[str, object] | None = None,
) -> logging.LogRecord:
    rec = logging.LogRecord(
        name=name,
        level=level,
        pathname="x.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in (extras or {}).items():
        setattr(rec, k, v)
    return rec


class TestJsonFormatter:
    def test_minimal_record_emits_core_fields(self) -> None:
        out = JsonFormatter().format(_record())
        payload = json.loads(out)
        assert payload["msg"] == "hello"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "ccbot.test"
        assert "ts" in payload

    def test_extras_are_hoisted_to_top_level(self) -> None:
        out = JsonFormatter().format(
            _record(extras={"event": "queue_started", "user_id": 42, "depth": 7})
        )
        payload = json.loads(out)
        assert payload["event"] == "queue_started"
        assert payload["user_id"] == 42
        assert payload["depth"] == 7

    def test_non_serializable_extra_falls_back_to_repr(self) -> None:
        class Weird:
            def __repr__(self) -> str:
                return "<Weird>"

        out = JsonFormatter().format(_record(extras={"obj": Weird()}))
        payload = json.loads(out)
        assert payload["obj"] == "<Weird>"

    def test_standard_attrs_not_duplicated(self) -> None:
        out = JsonFormatter().format(_record())
        payload = json.loads(out)
        # `args`, `created`, etc. should not appear at the top level.
        for k in ("args", "created", "lineno", "filename", "module"):
            assert k not in payload


def test_file_logging_does_not_duplicate_records_to_stderr(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CCBOT_DIR", str(tmp_path))
    stderr = StringIO()
    monkeypatch.setattr(sys, "stderr", stderr)
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    old_level = root.level
    try:
        configure_logging()
        handlers = list(root.handlers)
        assert len(handlers) == 1
        assert isinstance(handlers[0], TimedRotatingFileHandler)

        root.warning("single destination probe")
        handlers[0].flush()
        assert (tmp_path / "logs" / "bot.log").read_text().count(
            "single destination probe"
        ) == 1
        assert "single destination probe" not in stderr.getvalue()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in old_handlers:
            root.addHandler(handler)
        root.setLevel(old_level)
