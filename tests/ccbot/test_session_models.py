"""Tests for session_models — Session / WindowState dataclass round-trips."""

from ccbot.session_models import Session, WindowState


class TestWindowState:
    def test_empty_round_trip(self) -> None:
        ws = WindowState()
        assert WindowState.from_dict(ws.to_dict()).to_dict() == ws.to_dict()

    def test_to_dict_omits_empty_window_name(self) -> None:
        ws = WindowState(session_id="abc", cwd="/tmp")
        d = ws.to_dict()
        assert "window_name" not in d
        assert d == {"session_id": "abc", "cwd": "/tmp"}

    def test_to_dict_includes_window_name(self) -> None:
        ws = WindowState(session_id="abc", cwd="/tmp", window_name="proj")
        assert ws.to_dict()["window_name"] == "proj"


class TestSession:
    def test_new_id_is_8_hex(self) -> None:
        sid = Session.new_id()
        assert len(sid) == 8
        int(sid, 16)  # raises if not hex

    def test_round_trip_preserves_token_usage_total(self) -> None:
        s = Session(
            id="abc",
            name="test",
            window_id="@5",
            workdir="/tmp",
            token_usage_total=12345,
        )
        restored = Session.from_dict(s.to_dict())
        assert restored.token_usage_total == 12345
        assert restored.window_id == "@5"

    def test_from_dict_normalizes_invalid_state(self) -> None:
        s = Session.from_dict({"id": "x", "state": "bogus"})
        assert s.state == "active"

    def test_from_dict_ignores_legacy_alert_field(self) -> None:
        # Legacy state.json files may still carry ``alerted_token_thresholds``;
        # from_dict tolerates extra keys silently.
        s = Session.from_dict(
            {"id": "x", "alerted_token_thresholds": [100_000, 200_000]}
        )
        assert s.id == "x"
