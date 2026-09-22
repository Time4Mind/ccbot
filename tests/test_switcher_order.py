"""Regression test: the inline session switcher renders its buttons oldest ->
newest (by created_at). A session keeps a stable slot as newer ones are
appended to the right, rather than jumping around with the active-first /
by-name order that ``list_user_sessions`` returns.
"""

from __future__ import annotations

from ccbot.handlers import bg_status
from ccbot.handlers.switcher import build_switcher_keyboard
from ccbot.session import session_manager
from ccbot.session_models import Session


def _session(sid: str, name: str, created_at: float) -> Session:
    return Session(
        id=sid,
        name=name,
        window_id=f"@{sid}",
        workdir="/root/x",
        state="active",
        created_at=created_at,
    )


def _button_names(markup: object) -> list[str]:
    rows = markup.inline_keyboard  # type: ignore[union-attr]
    names: list[str] = []
    for row in rows:
        for btn in row:
            if btn.text == "+ new":
                continue
            # Strip leading state/selection glyphs -> plain name.
            names.append(btn.text.split(" ")[-1])
    return names


def test_switcher_orders_oldest_to_newest() -> None:
    saved = dict(session_manager.sessions)
    saved_active = dict(session_manager.active_sessions)
    try:
        session_manager.sessions.clear()
        # Insert out of chronological order on purpose.
        session_manager.sessions["b"] = _session("b", "middle", created_at=200.0)
        session_manager.sessions["c"] = _session("c", "newest", created_at=300.0)
        session_manager.sessions["a"] = _session("a", "oldest", created_at=100.0)
        session_manager.active_sessions.clear()
        session_manager.active_sessions[42] = "c"

        markup = build_switcher_keyboard(42)
        assert markup is not None
        assert _button_names(markup) == ["oldest", "middle", "newest"]
    finally:
        session_manager.sessions.clear()
        session_manager.sessions.update(saved)
        session_manager.active_sessions.clear()
        session_manager.active_sessions.update(saved_active)


def test_restored_session_flies_in_as_newest() -> None:
    """A session brought back via set_session_window (restore / re-bind) gets a
    fresh created_at, so it slots to the far right of the switcher."""
    saved = dict(session_manager.sessions)
    saved_active = dict(session_manager.active_sessions)
    try:
        session_manager.sessions.clear()
        session_manager.sessions["a"] = _session("a", "oldest", created_at=100.0)
        session_manager.sessions["b"] = _session("b", "middle", created_at=200.0)
        # Was the oldest and archived; restore it onto a new window.
        restored = _session("z", "restored", created_at=50.0)
        restored.state = "archived"
        session_manager.sessions["z"] = restored
        session_manager.active_sessions.clear()

        session_manager.set_session_window("z", "@99")

        assert session_manager.sessions["z"].state == "active"
        assert session_manager.sessions["z"].created_at > 200.0
        markup = build_switcher_keyboard(42)
        assert markup is not None
        assert _button_names(markup) == ["oldest", "middle", "restored"]
    finally:
        session_manager.sessions.clear()
        session_manager.sessions.update(saved)
        session_manager.active_sessions.clear()
        session_manager.active_sessions.update(saved_active)


def test_switcher_uses_status_without_arbitrary_colour_emoji() -> None:
    """Every live session has one semantic lifecycle marker."""
    saved = dict(session_manager.sessions)
    saved_active = dict(session_manager.active_sessions)
    saved_bg = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    try:
        session_manager.sessions.clear()
        session_manager.sessions["a"] = _session("a", "current", created_at=100.0)
        session_manager.sessions["b"] = _session("b", "background", created_at=200.0)
        session_manager.active_sessions.clear()
        session_manager.active_sessions[42] = "a"
        bg_status._bg.clear()
        bg_status.update_status(42, "b", "working")

        markup = build_switcher_keyboard(42)

        assert markup is not None
        labels = [button.text for row in markup.inline_keyboard for button in row]
        assert "✓ current" in labels
        assert "🔶 background" in labels
        assert all("🟩" not in label and "🟨" not in label for label in labels)
    finally:
        session_manager.sessions.clear()
        session_manager.sessions.update(saved)
        session_manager.active_sessions.clear()
        session_manager.active_sessions.update(saved_active)
        bg_status._bg.clear()
        bg_status._bg.update(saved_bg)


def test_attention_states_share_one_exclamation_marker() -> None:
    saved_bg = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    try:
        bg_status._bg.clear()
        for sid, status in (
            ("question", "needs_action"),
            ("broken", "error"),
            ("stuck", "stalled"),
        ):
            bg_status.update_status(42, sid, status)  # type: ignore[arg-type]
            assert bg_status.status_emoji(42, sid) == "❗"
    finally:
        bg_status._bg.clear()
        bg_status._bg.update(saved_bg)


def test_error_marker_is_sticky_until_explicit_user_recovery() -> None:
    saved_bg = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    try:
        bg_status._bg.clear()
        bg_status.update_status(42, "broken", "error")

        assert bg_status.update_status(42, "broken", "working") is False
        assert bg_status.status_emoji(42, "broken") == "❗"

        assert bg_status.update_status(42, "broken", "working", force=True) is True
        assert bg_status.status_emoji(42, "broken") == "🔶"
    finally:
        bg_status._bg.clear()
        bg_status._bg.update(saved_bg)


def test_finished_marker_becomes_seen_only_on_explicit_acknowledgement() -> None:
    saved_bg = {uid: dict(bucket) for uid, bucket in bg_status._bg.items()}
    try:
        bg_status._bg.clear()
        assert bg_status.status_emoji(42, "legacy-idle") == ""

        bg_status.update_status(42, "done", "finished")
        assert bg_status.status_emoji(42, "done") == "✅"

        assert bg_status.record_finished_view(42, "done") is False
        assert bg_status.status_emoji(42, "done") == "✅"
        assert bg_status.record_finished_view(42, "done") is True
        assert bg_status.status_emoji(42, "done") == ""
        assert bg_status.record_finished_view(42, "done") is False
    finally:
        bg_status._bg.clear()
        bg_status._bg.update(saved_bg)
