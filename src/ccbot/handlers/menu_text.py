"""Text renderers for the Menu and Settings Telegram screens.

The stable ``handlers.menu`` module re-exports these functions.
"""

from __future__ import annotations

from ..i18n import t
from ..session import session_manager
from .menu_settings_data import Screen, _GROUP_TEXT_KEYS


__all__ = [
    "render_settings_text",
    "render_settings_group_text",
    "render_more_text",
]


def _settings_hard_breaks(text: str) -> str:
    """Make single newlines hard breaks while preserving blank paragraphs.

    CommonMark treats a bare newline as whitespace in Rich Markdown. Two
    trailing spaces keep the intended settings layout; plain-text fallback
    still displays the same newlines and merely carries invisible spaces.
    """
    lines = text.split("\n")
    for index, line in enumerate(lines[:-1]):
        if line.strip() and lines[index + 1].strip():
            lines[index] = f"{line.rstrip()}  "
    return "\n".join(lines)


def render_settings_text(user_id: int) -> str:
    """Body text shown on the top-level Settings screen."""
    s = session_manager.get_user_settings(user_id)
    return _settings_hard_breaks(
        t(
            user_id,
            "settings.body",
            agent=session_manager.agent_backend.capitalize(),
            language=s.get("language", "en"),
            live_lag=int(s.get("live_lag", 4)),
            voice=s.get("voice", "auto"),
        )
    )


def render_settings_group_text(user_id: int, screen: Screen) -> str:
    """Body text for a settings group sub-screen."""
    key = _GROUP_TEXT_KEYS.get(screen, "settings.title")
    return _settings_hard_breaks(t(user_id, key))


def render_more_text(user_id: int) -> str:
    """Body text shown above the menu grid."""
    sess = session_manager.get_active_session(user_id)
    if sess is None:
        return t(user_id, "menu.empty")
    return t(user_id, "menu.active", name=sess.name or sess.id)
