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
    CB_ST_ARCHIVE_AI,
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
    CB_ST_LTERM,
    CB_ST_PAGESIZE,
    CB_ST_SPOILER_LINES,
    CB_ST_CAPTURE,
    CB_ST_OPTION,
    CB_ST_PROFILE,
    CB_ST_PREPROCESS,
    CB_ST_PREPROCESS_INSTRUCTION,
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
    "_settings_option_grid",
    "_settings_capture_grid",
    "_settings_profile_grid",
    "_settings_haiku_grid",
    "_settings_archive_ai_grid",
    "_settings_bg_notify_grid",
    "_settings_pagesize_grid",
    "_settings_spoiler_lines_grid",
    "_settings_weeklyday_grid",
    "_settings_preprocessing_mode_grid",
    "_settings_preprocessing_instruction_grid",
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
    if value_key in ("spoiler_command_lines", "spoiler_result_lines"):
        return f"{int(cur)} lines" if cur else "?"  # type: ignore[arg-type]
    if value_key in ("option_button_screenshot", "option_button_terminal"):
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key == "screenshot_capture_kib":
        return f"{cur} KiB"
    if value_key == "screenshot_profile":
        return t(user_id, f"screenshot.profile.{cur}")
    if value_key in ("bg_notify_finished", "bg_notify_error", "bg_notify_needs_action"):
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key in ("haiku_naming", "archive_ai_description"):
        return t(user_id, "screens.on") if cur else t(user_id, "screens.off")
    if value_key == "preprocessing_mode":
        return t(user_id, f"preprocessing.mode.{cur or 'off'}")
    if value_key == "preprocessing_instruction":
        return t(
            user_id,
            "preprocessing.instruction.custom"
            if str(cur or "").strip()
            else "preprocessing.instruction.builtin",
        )
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
    groups_by_key = {key: (lk, sc, vk) for key, lk, sc, vk in _SETTINGS_GROUPS}
    rows: list[list[InlineKeyboardButton]] = []
    for member_key in members:
        if member_key not in groups_by_key:
            continue
        label_key, _sub_screen, _value_key = groups_by_key[member_key]
        label = t(user_id, label_key)
        rows.append(
            [
                InlineKeyboardButton(
                    label,
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
            for v in (2, 4, 8)
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
            for v in ("auto", "parakeet", "whisper", "apple", "off")
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


def _settings_preprocessing_mode_grid(
    user_id: int,
) -> list[list[InlineKeyboardButton]]:
    cur = str(
        session_manager.get_user_settings(user_id).get("preprocessing_mode", "off")
    )
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"preprocessing.mode.{value}"), cur == value),
                callback_data=f"{CB_ST_PREPROCESS}{value}",
            )
            for value in ("off", "voice", "all")
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("preprocessing_mode"),
            )
        ],
    ]


def _settings_preprocessing_instruction_grid(
    user_id: int,
) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton(
                t(user_id, "preprocessing.instruction.edit"),
                callback_data=f"{CB_ST_PREPROCESS_INSTRUCTION}edit",
            )
        ],
        [
            InlineKeyboardButton(
                t(user_id, "preprocessing.instruction.reset"),
                callback_data=f"{CB_ST_PREPROCESS_INSTRUCTION}reset",
            )
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("preprocessing_instruction"),
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
    cur_cmd = settings.get("local_terminal_cmd", "")

    rows: list[list[InlineKeyboardButton]] = []
    # Visibility is configured in Option buttons. This screen only selects
    # the Linux emulator/template where the platform needs one.
    if platform.system() == "Linux":
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


def _settings_option_grid(user_id: int, key: str) -> list[list[InlineKeyboardButton]]:
    cur = bool(session_manager.get_user_settings(user_id).get(key, False))
    suffix = "screenshot" if key == "option_button_screenshot" else "terminal"
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.on"), cur),
                callback_data=f"{CB_ST_OPTION}{suffix}:on",
            ),
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.off"), not cur),
                callback_data=f"{CB_ST_OPTION}{suffix}:off",
            ),
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb(key),
            )
        ],
    ]


def _settings_capture_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = int(
        session_manager.get_user_settings(user_id).get("screenshot_capture_kib", 48)
    )
    return [
        [
            InlineKeyboardButton(
                _highlight(f"{value} KiB", cur == value),
                callback_data=f"{CB_ST_CAPTURE}{value}",
            )
            for value in (48, 64, 86)
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("screenshot_capture_kib"),
            )
        ],
    ]


def _settings_profile_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = str(
        session_manager.get_user_settings(user_id).get("screenshot_profile", "full8")
    )
    values = ("full8", "compact8", "fullcolor")
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"screenshot.profile.{value}"), cur == value),
                callback_data=f"{CB_ST_PROFILE}{value}",
            )
        ]
        for value in values
    ] + [
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("screenshot_profile"),
            )
        ]
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


def _settings_archive_ai_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = bool(
        session_manager.get_user_settings(user_id).get("archive_ai_description", False)
    )
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.on"), cur),
                callback_data=f"{CB_ST_ARCHIVE_AI}on",
            ),
            InlineKeyboardButton(
                _highlight(t(user_id, "screens.off"), not cur),
                callback_data=f"{CB_ST_ARCHIVE_AI}off",
            ),
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb("archive_ai_description"),
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


def _settings_spoiler_lines_grid(
    user_id: int, setting_key: str
) -> list[list[InlineKeyboardButton]]:
    raw = session_manager.get_user_settings(user_id).get(setting_key, 10)
    try:
        cur = int(raw)
    except (TypeError, ValueError):
        cur = 10
    if cur not in (5, 10, 30, 60):
        cur = 10
    return [
        [
            InlineKeyboardButton(
                _highlight(str(v), cur == v),
                callback_data=(
                    f"{CB_ST_SPOILER_LINES}"
                    f"{'command' if setting_key == 'spoiler_command_lines' else 'result'}:{v}"
                ),
            )
            for v in (5, 10, 30, 60)
        ],
        [
            InlineKeyboardButton(
                t(user_id, "btn.back"),
                callback_data=_parent_cat_cb(setting_key),
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
