"""Inline keyboard builders for Telegram Settings screens.

The functions are behavior-preserving extractions from ``handlers.menu`` and
continue to be re-exported by that compatibility facade.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton

from ..i18n import LANGUAGES, t
from ..session import session_manager
from .callback_data import (
    CB_MM_SETTINGS,
    CB_ST_APPROVE,
    CB_ST_AGENT,
    CB_ST_BACK,
    CB_ST_BGNOTIFY,
    CB_ST_CAT,
    CB_ST_CHIST,
    CB_ST_GRP,
    CB_ST_HAIKU,
    CB_ST_IDLE,
    CB_ST_LANG,
    CB_ST_LAG,
    CB_ST_LCLAUDE,
    CB_ST_LOCAL,
    CB_ST_LTERM,
    CB_ST_PAGESIZE,
    CB_ST_SCREENS,
    CB_ST_VOICE,
    CB_ST_WDAY,
)
from .menu_settings_data import SETTINGS_CATEGORIES, WEEKDAYS, _SETTINGS_GROUPS


__all__ = [
    "_highlight",
    "_parent_cat_cb",
    "_format_setting_value",
    "_settings_main_grid",
    "_settings_category_grid",
    "_settings_lag_grid",
    "_settings_voice_grid",
    "_settings_language_grid",
    "_settings_agent_grid",
    "_settings_approve_grid",
    "_settings_idle_archive_grid",
    "_settings_local_grid",
    "_settings_cardhist_grid",
    "_settings_screens_grid",
    "_settings_haiku_grid",
    "_settings_bg_notify_grid",
    "_settings_pagesize_grid",
    "_settings_weeklyday_grid",
]


def _highlight(label: str, active: bool) -> str:
    return f"• {label}" if active else label


def _parent_cat_cb(group_key: str) -> str:
    """Callback the Back row of an individual setting points at — the
    CATEGORY sub-screen that contains ``group_key`` (per pivot #53
    feedback: tapping Back was dumping users at the top-level Settings
    instead of the relevant category).
    """
    for _label, cat_screen, members in SETTINGS_CATEGORIES:
        if group_key in members:
            return f"{CB_ST_CAT}{cat_screen}"
    return CB_MM_SETTINGS


def _format_setting_value(user_id: int, value_key: str, cur: object) -> str:
    """Format a single setting's current value for display in buttons."""
    if value_key == "live_lag":
        return f"{int(cur)}s" if cur is not None else "?"  # type: ignore[arg-type]
    if value_key == "weekly_reset_day":
        return t(user_id, f"day.{cur}") if cur else "?"
    if value_key == "auto_approve":
        return t(user_id, f"approve.{cur}") if cur else "?"
    if value_key == "session_idle_hours":
        try:
            hours = int(str(cur))
        except (TypeError, ValueError):
            return "?"
        return t(user_id, "settings.value.hours", value=hours)
    if value_key == "local_terminal":
        return t(user_id, f"local.{cur}") if cur else "?"
    if value_key == "card_history":
        return f"{int(cur)} turns" if cur else "?"  # type: ignore[arg-type]
    if value_key == "card_page_lines":
        return f"{int(cur)} lines" if cur else "?"  # type: ignore[arg-type]
    if value_key == "card_inline_screenshots":
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key in ("bg_notify_finished", "bg_notify_error", "bg_notify_needs_action"):
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key == "haiku_naming":
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key == "agent_backend":
        return str(cur).capitalize()
    return str(cur) if cur is not None else "?"


def _settings_main_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """Top-level Settings screen — category selector.

    Settings became too many for a flat list (user feedback). Each
    category opens a sub-screen listing its members. Languages /
    auto-approve land in the 'Behavior' category for now.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for label_key, screen_name, _members in SETTINGS_CATEGORIES:
        label = t(user_id, label_key)
        rows.append(
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"{CB_ST_CAT}{screen_name}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_ST_BACK)]
    )
    return rows


def _settings_category_grid(
    user_id: int, screen_name: str
) -> list[list[InlineKeyboardButton]]:
    """Sub-screen for one category: its member settings as buttons."""
    members: tuple[str, ...] = ()
    for _label_key, sname, m in SETTINGS_CATEGORIES:
        if sname == screen_name:
            members = m
            break
    s = session_manager.get_user_settings(user_id)
    groups_by_key = {key: (lk, sc, vk) for key, lk, sc, vk in _SETTINGS_GROUPS}
    rows: list[list[InlineKeyboardButton]] = []
    for member_key in members:
        if member_key not in groups_by_key:
            continue
        label_key, _sub_screen, value_key = groups_by_key[member_key]
        cur = (
            session_manager.agent_backend
            if value_key == "agent_backend"
            else s.get(value_key, "")
        )
        label = t(user_id, label_key)
        value_str = _format_setting_value(user_id, value_key, cur)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{label}: {value_str}",
                    callback_data=f"{CB_ST_GRP}{member_key}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=f"{CB_ST_CAT}settings"
            )
        ]
    )
    return rows


def _settings_lag_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = int(session_manager.get_user_settings(user_id).get("live_lag", 4))
    return [
        [
            InlineKeyboardButton(
                _highlight(f"{v}s", cur == v),
                callback_data=f"{CB_ST_LAG}{v}",
            )
            for v in (0, 2, 4, 8)
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=_parent_cat_cb("live_lag")
            )
        ],
    ]


def _settings_voice_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("voice", "auto")
    return [
        [
            InlineKeyboardButton(
                _highlight(v, cur == v),
                callback_data=f"{CB_ST_VOICE}{v}",
            )
            for v in ("auto", "whisper", "apple", "off")
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=_parent_cat_cb("voice")
            )
        ],
    ]


def _settings_language_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("language", "en")
    return [
        [
            InlineKeyboardButton(
                _highlight(f"{label}", cur == code),
                callback_data=f"{CB_ST_LANG}{code}",
            )
            for code, label in LANGUAGES
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=_parent_cat_cb("language")
            )
        ],
    ]


def _settings_agent_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.agent_backend
    return [
        [
            InlineKeyboardButton(
                _highlight(name.capitalize(), cur == name),
                callback_data=f"{CB_ST_AGENT}{name}",
            )
            for name in ("claude", "codex")
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("agent_backend"),
            )
        ],
    ]


def _settings_approve_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("auto_approve", "off")
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"approve.{v}"), cur == v),
                callback_data=f"{CB_ST_APPROVE}{v}",
            )
            for v in ("off", "on")
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=_parent_cat_cb("auto_approve")
            )
        ],
    ]


def _settings_idle_archive_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    from ..session import DEFAULT_IDLE_ARCHIVE_HOURS, IDLE_ARCHIVE_HOUR_CHOICES

    raw = session_manager.get_user_settings(user_id).get(
        "session_idle_hours", DEFAULT_IDLE_ARCHIVE_HOURS
    )
    try:
        cur = int(raw)
    except (TypeError, ValueError):
        cur = DEFAULT_IDLE_ARCHIVE_HOURS
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "settings.value.hours", value=v), cur == v),
                callback_data=f"{CB_ST_IDLE}{v}",
            )
            for v in IDLE_ARCHIVE_HOUR_CHOICES
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("session_idle_hours"),
            )
        ],
    ]


def _settings_local_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    import platform

    from ..local_terminal import LINUX_TEMPLATES, detect_linux_emulators

    settings = session_manager.get_user_settings(user_id)
    cur = settings.get("local_terminal", "off")
    cur_cmd = settings.get("local_terminal_cmd", "")

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"local.{v}"), cur == v),
                callback_data=f"{CB_ST_LOCAL}{v}",
            )
            for v in ("off", "manual", "auto")
        ]
    )

    # Linux + a terminal-enabled mode: surface the emulator picker.
    # Empty list → fall back to the claude-typed snippet flow.
    if cur in ("manual", "auto") and platform.system() == "Linux":
        detected = detect_linux_emulators()
        if detected:
            for i in range(0, len(detected), 2):
                row: list[InlineKeyboardButton] = []
                for name in detected[i : i + 2]:
                    selected = cur_cmd == LINUX_TEMPLATES[name]
                    row.append(
                        InlineKeyboardButton(
                            _highlight(name, selected),
                            callback_data=f"{CB_ST_LTERM}{name}",
                        )
                    )
                rows.append(row)
        rows.append(
            [
                InlineKeyboardButton(
                    t(user_id, "settings.local.claude_help"),
                    callback_data=CB_ST_LCLAUDE,
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("local_terminal"),
            )
        ]
    )
    return rows


def _settings_cardhist_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """How many end_turn boundaries to seed into a fresh live card.

    Fixed row of values 10 / 20 / 50 / 100. Deep history beyond this is
    always reachable via ``/history`` regardless of the chosen value.
    """
    raw = session_manager.get_user_settings(user_id).get("card_history", 20)
    try:
        cur = int(raw)
    except (TypeError, ValueError):
        cur = 20
    return [
        [
            InlineKeyboardButton(
                _highlight(str(v), cur == v),
                callback_data=f"{CB_ST_CHIST}{v}",
            )
            for v in (10, 20, 50, 100)
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("card_history"),
            )
        ],
    ]


def _settings_screens_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """Inline-screenshots on/off toggle. Settings body explains the
    The screenshot is the final media block of the Rich Markdown card.
    """
    cur = bool(
        session_manager.get_user_settings(user_id).get("card_inline_screenshots", False)
    )
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.on"), cur),
                callback_data=f"{CB_ST_SCREENS}on",
            ),
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.off"), not cur),
                callback_data=f"{CB_ST_SCREENS}off",
            ),
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("card_inline_screenshots"),
            )
        ],
    ]


def _settings_haiku_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """Lightweight-model auto-rename on/off toggle.

    When *off*, new sessions keep the directory-basename name forever
    (``workdir``, ``workdir-2``, ...). When *on*, a one-shot backend-specific
    model call on the first user message ≥20 chars renames the session.
    """
    cur = bool(session_manager.get_user_settings(user_id).get("haiku_naming", True))
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.on"), cur),
                callback_data=f"{CB_ST_HAIKU}on",
            ),
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.off"), not cur),
                callback_data=f"{CB_ST_HAIKU}off",
            ),
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=_parent_cat_cb("haiku_naming")
            )
        ],
    ]


def _settings_bg_notify_grid(
    user_id: int, key: str, back_to: str
) -> list[list[InlineKeyboardButton]]:
    """Simple on/off toggle for one bg_notify_* setting.

    ``key`` is one of bg_notify_finished / _error / _needs_action.
    ``back_to`` is the screen name to return to (the parent category).
    """
    cur = bool(session_manager.get_user_settings(user_id).get(key, True))
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.on"), cur),
                callback_data=f"{CB_ST_BGNOTIFY}{key}:on",
            ),
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.off"), not cur),
                callback_data=f"{CB_ST_BGNOTIFY}{key}:off",
            ),
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"), callback_data=f"{CB_ST_CAT}{back_to}"
            )
        ],
    ]


def _settings_pagesize_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """Max page size in logical \\n-delimited lines.

    Fixed row 10 / 20 / 40 / 70. Smart anchor chunking with ±5 lines
    overshoot handles single events that exceed the budget without
    breaking mid-sentence / mid-word.
    """
    raw = session_manager.get_user_settings(user_id).get("card_page_lines", 20)
    try:
        cur = int(raw)
    except (TypeError, ValueError):
        cur = 20
    return [
        [
            InlineKeyboardButton(
                _highlight(str(v), cur == v),
                callback_data=f"{CB_ST_PAGESIZE}{v}",
            )
            for v in (10, 20, 40, 70)
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("card_page_lines"),
            )
        ],
    ]


def _settings_weeklyday_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("weekly_reset_day", "mon")
    rows: list[list[InlineKeyboardButton]] = []
    # 4 + 3 layout fits comfortably on a phone.
    week = list(WEEKDAYS)
    for chunk_start in (0, 4):
        chunk = week[chunk_start : chunk_start + 4]
        rows.append(
            [
                InlineKeyboardButton(
                    _highlight(t(user_id, f"day.{d}"), cur == d),
                    callback_data=f"{CB_ST_WDAY}{d}",
                )
                for d in chunk
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("weekly_reset_day"),
            )
        ]
    )
    return rows
