from ccbot.handlers import bg_status
from ccbot.handlers.card_layout import _render_card
from ccbot.handlers.card_types import CardState, Event
from ccbot.session_models import Session


def test_prior_final_header_follows_running_session(monkeypatch):
    monkeypatch.setattr(bg_status, "_bg", {})
    bg_status.update_status(42, "session", "working")
    session = Session(id="session", name="work", window_id="@1", workdir="/tmp")
    snapshot = CardState(
        completion_marker_pending=True,
        events=[Event("final_text", "previous answer", 1.0)],
    )
    text = _render_card(session, snapshot, user_id=42)
    assert text.startswith("🔶 *work*")
    assert "previous answer" in text
