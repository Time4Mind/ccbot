"""Tests for logging_setup — JsonFormatter shape + extras hoisting."""

import json
import logging

from ccbot.logging_setup import JsonFormatter


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
