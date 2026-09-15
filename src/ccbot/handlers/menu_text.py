"""Text renderers for the Menu and Settings Telegram screens.

The stable ``handlers.menu`` module re-exports these functions.
"""

from __future__ import annotations

from ..i18n import t
from ..session import session_manager
from .menu_settings import _format_setting_value
from .menu_settings_data import (
    SETTINGS_CATEGORIES,
    Screen,
    _GROUP_TEXT_KEYS,
    _SETTINGS_GROUPS,
)


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


def _table_cell(value: object) -> str:
    """Keep settings labels and values literal inside a GFM table cell."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _table(headers: tuple[str, str], rows: list[tuple[str, str]]) -> str:
    lines = [
        f"| {_table_cell(headers[0])} | {_table_cell(headers[1])} |",
        "|---|---|",
    ]
    lines.extend(
        f"| {_table_cell(left)} | {_table_cell(right)} |" for left, right in rows
    )
    return "\n".join(lines)


def _category(screen: Screen) -> tuple[str, tuple[str, ...]] | None:
    for label_key, category_screen, members in SETTINGS_CATEGORIES:
        if category_screen == screen:
            return label_key, members
    return None


def _groups_by_key() -> dict[str, tuple[str, str]]:
    return {
        key: (label_key, value_key)
        for key, label_key, _screen, value_key in _SETTINGS_GROUPS
    }


def render_settings_text(user_id: int) -> str:
    """Body text shown on the top-level Settings screen."""
    group_catalog = _groups_by_key()
    rows = []
    for category_label_key, _screen, members in SETTINGS_CATEGORIES:
        member_labels = [
            t(user_id, group_catalog[key][0]) for key in members if key in group_catalog
        ]
        rows.append((t(user_id, category_label_key), ", ".join(member_labels)))
    return "\n\n".join(
        (
            t(user_id, "settings.title"),
            _table(
                (
                    t(user_id, "settings.table.section"),
                    t(user_id, "settings.table.contents"),
                ),
                rows,
            ),
            t(user_id, "settings.table.hint"),
        )
    )


def render_settings_group_text(user_id: int, screen: Screen) -> str:
    """Body text for a settings group sub-screen."""
    key = _GROUP_TEXT_KEYS.get(screen, "settings.title")
    body = _settings_hard_breaks(t(user_id, key))
    if screen == "settings_preprocessing_instruction":
        from ..request_preprocessing import DEFAULT_PREPROCESSING_INSTRUCTION

        configured = str(
            session_manager.get_user_settings(user_id).get(
                "preprocessing_instruction", ""
            )
        ).strip()
        instruction = configured or DEFAULT_PREPROCESSING_INSTRUCTION
        return f"{body}\n\n```text\n{instruction}\n```"
    category = _category(screen)
    if category is None or len(category[1]) <= 1:
        return body

    current = session_manager.get_user_settings(user_id)
    group_catalog = _groups_by_key()
    rows = []
    for member_key in category[1]:
        group = group_catalog.get(member_key)
        if group is None:
            continue
        label_key, value_key = group
        value = (
            session_manager.agent_backend
            if value_key == "agent_backend"
            else current.get(value_key, "")
        )
        rows.append(
            (
                t(user_id, label_key),
                _format_setting_value(user_id, value_key, value),
            )
        )
    return "\n\n".join(
        (
            body,
            _table(
                (
                    t(user_id, "settings.table.button")
                    if screen == "settings_cat_options"
                    else t(user_id, "settings.table.setting"),
                    t(user_id, "settings.table.show")
                    if screen == "settings_cat_options"
                    else t(user_id, "settings.table.current"),
                ),
                rows,
            ),
        )
    )


def render_more_text(user_id: int) -> str:
    """Body text shown above the menu grid."""
    sess = session_manager.get_active_session(user_id)
    if sess is None:
        return t(user_id, "menu.empty")
    return t(user_id, "menu.active", name=sess.name or sess.id)
