"""User-visible identity metadata in the live-card header."""

from unittest.mock import AsyncMock

import pytest

from ccbot.handlers import card_terminal
from ccbot.handlers import bg_status
from ccbot.handlers.card_layout import _render_card
from ccbot.handlers.card_types import CardState
from ccbot.session_models import Session


def test_card_header_omits_model_but_context_row_keeps_it() -> None:
    sess = Session(
        id="options",
        name="options cleanup",
        workdir="/tmp/ccbot",
        state="active",
        backend="codex",
    )
    state = CardState(
        last_event_ts=1.0,
        agent_model="gpt-5.6-sol",
        reasoning_effort="medium",
        context_pct=42,
    )

    card = _render_card(sess, state)
    header = card.splitlines()[0]

    assert "· ccbot ·" in header
    assert "5.6-sol" not in header
    assert card.endswith("─── 5.6-sol med: 42% ───")


def test_card_context_row_shows_session_model_and_effort() -> None:
    sess = Session(id="usage", name="usage", backend="codex", state="active")
    state = CardState(
        agent_model="gpt-6-sol",
        reasoning_effort="high",
        context_pct=42,
    )

    card = _render_card(sess, state)

    assert card.endswith("─── 6-sol high: 42% ───")
    assert "context: 42%" not in card


@pytest.mark.parametrize("session_state", ["active", "idle"])
def test_card_shows_model_without_context_percentage(session_state: str) -> None:
    sess = Session(id="usage", name="usage", backend="codex", state=session_state)
    state = CardState(agent_model="gpt-6-sol", reasoning_effort="high")

    card = _render_card(sess, state)

    assert card.endswith("─── 6-sol high ───")


def test_card_shows_known_model_without_effort_or_context_percentage() -> None:
    sess = Session(id="usage", name="usage", backend="codex", state="active")
    state = CardState(agent_model="gpt-6-sol")

    card = _render_card(sess, state)

    assert card.endswith("─── 6-sol ───")


def test_interactive_card_keeps_model_row_without_context_percentage() -> None:
    sess = Session(id="picker", name="picker", backend="codex", state="active")
    state = CardState(
        agent_model="gpt-6-astra",
        reasoning_effort="medium",
        in_kb_mode=True,
        kb_prompt="Choose an option",
    )

    card = _render_card(sess, state)

    assert "⌨ *Waiting for your input:*" in card
    assert card.endswith("─── 6-astra med ───")


def test_card_context_row_keeps_label_when_identity_is_unknown() -> None:
    sess = Session(id="usage", name="usage", backend="claude", state="active")
    state = CardState(context_pct=42)

    card = _render_card(sess, state)

    assert card.endswith("─── context: 42% ───")


def test_card_context_row_recovers_saved_percent_after_restart() -> None:
    sess = Session(id="restored-usage", name="usage", backend="codex", state="active")
    state = CardState(agent_model="gpt-6-sol", reasoning_effort="high")
    bg_status.set_context_pct(42, sess.id, 20)
    try:
        card = _render_card(sess, state, user_id=42)
    finally:
        bg_status.clear_for_user_session(42, sess.id)

    assert card.endswith("─── 6-sol high: 20% ───")


@pytest.mark.asyncio
async def test_codex_identity_recovers_from_rollout_when_busy_pane_hides_footer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(
        '{"type":"turn_context","payload":{"model":"gpt-6-sol","effort":"high"}}\n'
    )
    sess = Session(
        id="restored-identity",
        name="usage",
        backend="codex",
        window_id="@restored",
        claude_session_id="codex-session-id",
        workdir=str(tmp_path),
    )
    state = CardState()
    monkeypatch.setattr(
        card_terminal, "capture_session_pane", AsyncMock(return_value="Working...")
    )
    monkeypatch.setattr(
        "ccbot.codex_session_io.build_session_file_path", lambda *_args: rollout
    )

    assert await card_terminal.sync_card_identity(sess, state)
    assert "6-sol high" not in _render_card(sess, state, user_id=42).splitlines()[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("node_id", ["local", "worker-a"])
async def test_codex_identity_remains_out_of_header_on_each_node(
    monkeypatch: pytest.MonkeyPatch, node_id: str
) -> None:
    sess = Session(
        id=node_id,
        name="same session",
        backend="codex",
        node_id=node_id,
        window_id="@17" if node_id == "local" else "worker-a::@17",
    )
    state = CardState(last_event_ts=1.0)
    capture = AsyncMock(
        side_effect=[
            "› Ask anything\n\n  gpt-5.6-sol medium · /project",
            "› Ask anything\n\n  gpt-6-astra high · /project",
        ]
    )
    monkeypatch.setattr(card_terminal, "capture_session_pane", capture)

    assert await card_terminal.sync_card_identity(sess, state)
    first_header = _render_card(sess, state).splitlines()[0]
    assert "5.6-sol med" not in first_header

    assert await card_terminal.sync_card_identity(sess, state)
    changed_header = _render_card(sess, state).splitlines()[0]
    assert "6-astra high" not in changed_header


@pytest.mark.asyncio
@pytest.mark.parametrize("page", [0, None])
async def test_fast_identity_follows_live_mode_and_survives_partial_pane(
    monkeypatch, page
) -> None:
    sess = Session(id="fast", name="fast", backend="codex", window_id="@1")
    state = CardState(current_page_idx=page, context_pct=42)
    monkeypatch.setattr(
        card_terminal,
        "capture_session_pane",
        AsyncMock(
            side_effect=[
                "› Ask Codex to do anything\n\n  GPT-6.1-Sol high fast · ~/workdir",
                "Working...",
                "› Ask Codex to do anything\n\n  GPT-6.1-Sol high · ~/workdir",
            ]
        ),
    )

    assert await card_terminal.sync_card_identity(sess, state)
    assert _render_card(sess, state).endswith("─── 6.1-Sol high Fast: 42% ───")
    await card_terminal.sync_card_identity(sess, state)
    assert _render_card(sess, state).endswith("─── 6.1-Sol high Fast: 42% ───")
    assert await card_terminal.sync_card_identity(sess, state)
    assert _render_card(sess, state).endswith("─── 6.1-Sol high: 42% ───")
