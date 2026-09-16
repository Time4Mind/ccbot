"""User-visible identity metadata in the live-card header."""

from ccbot.handlers.card_layout import _render_card
from ccbot.handlers.card_types import CardState
from ccbot.session_models import Session


def test_card_header_shows_compact_model_and_effort_before_timestamp() -> None:
    sess = Session(
        id="options",
        name="options cleanup",
        workdir="/tmp/ccbot",
        state="active",
        backend="codex",
    )
    state = CardState(last_event_ts=1.0)
    # CardState does not expose these fields yet: assigning through the public
    # state object keeps this first TDD step a behavioral render failure rather
    # than an import/setup failure.
    state.agent_model = "gpt-5.6-sol"  # type: ignore[attr-defined]
    state.reasoning_effort = "medium"  # type: ignore[attr-defined]

    header = _render_card(sess, state).splitlines()[0]

    assert "· ccbot · 5.6-sol med ·" in header
