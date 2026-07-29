"""Regression test: the inline session switcher renders its buttons oldest ->
newest (by created_at). A session keeps a stable slot as newer ones are
appended to the right, rather than jumping around with the active-first /
by-name order that ``list_user_sessions`` returns.
"""

from __future__ import annotations

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
            # Strip leading marker glyphs ("✓ 🟦 ", "🟦 ") -> plain name
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
