"""Settings screen names and immutable menu catalog metadata.

This leaf module has no runtime state and is shared by keyboard and text
renderers. Names mirror the historical ``handlers.menu`` attributes.
"""

from __future__ import annotations

from typing import Literal

__all__ = [
    "Screen",
    "_SETTINGS_GROUPS",
    "SETTINGS_CATEGORIES",
    "WEEKDAYS",
    "_GROUP_TEXT_KEYS",
]

Screen = Literal[
    "main",
    "more",
    "settings",
    # Category sub-screens (group selector → category contents).
    "settings_cat_card",
    "settings_cat_notifications",
    "settings_cat_voice",
    "settings_cat_terminal",
    "settings_cat_behavior",
    # Individual setting sub-screens.
    "settings_lag",
    "settings_voice",
    "settings_language",
    "settings_agent",
    "settings_weeklyday",
    "settings_approve",
    "settings_local",
    "settings_cardhist",
    "settings_pagesize",
    "settings_screens",
    "settings_bg_notify_finished",
    "settings_bg_notify_error",
    "settings_bg_notify_needs_action",
    "settings_haiku",
    "settings_idle_archive",
]


_SETTINGS_GROUPS: tuple[tuple[str, str, str, str], ...] = (
    ("agent_backend", "settings.group.agent", "settings_agent", "agent_backend"),
    ("language", "settings.group.language", "settings_language", "language"),
    ("live_lag", "settings.group.live_lag", "settings_lag", "live_lag"),
    ("voice", "settings.group.voice", "settings_voice", "voice"),
    (
        "weekly_reset_day",
        "settings.group.weekly_reset_day",
        "settings_weeklyday",
        "weekly_reset_day",
    ),
    (
        "auto_approve",
        "settings.group.auto_approve",
        "settings_approve",
        "auto_approve",
    ),
    (
        "session_idle_hours",
        "settings.group.session_idle_hours",
        "settings_idle_archive",
        "session_idle_hours",
    ),
    (
        "local_terminal",
        "settings.group.local_terminal",
        "settings_local",
        "local_terminal",
    ),
    (
        "card_history",
        "settings.group.card_history",
        "settings_cardhist",
        "card_history",
    ),
    (
        "card_page_lines",
        "settings.group.card_page_lines",
        "settings_pagesize",
        "card_page_lines",
    ),
    (
        "card_inline_screenshots",
        "settings.group.card_inline_screenshots",
        "settings_screens",
        "card_inline_screenshots",
    ),
    (
        "bg_notify_finished",
        "settings.group.bg_notify_finished",
        "settings_bg_notify_finished",
        "bg_notify_finished",
    ),
    (
        "bg_notify_error",
        "settings.group.bg_notify_error",
        "settings_bg_notify_error",
        "bg_notify_error",
    ),
    (
        "bg_notify_needs_action",
        "settings.group.bg_notify_needs_action",
        "settings_bg_notify_needs_action",
        "bg_notify_needs_action",
    ),
    (
        "haiku_naming",
        "settings.group.haiku_naming",
        "settings_haiku",
        "haiku_naming",
    ),
)


SETTINGS_CATEGORIES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "settings.cat.card",
        "settings_cat_card",
        (
            "live_lag",
            "card_history",
            "card_page_lines",
            "card_inline_screenshots",
        ),
    ),
    (
        "settings.cat.notifications",
        "settings_cat_notifications",
        (
            "bg_notify_finished",
            "bg_notify_error",
            "bg_notify_needs_action",
            "weekly_reset_day",
        ),
    ),
    (
        "settings.cat.voice",
        "settings_cat_voice",
        ("voice",),
    ),
    (
        "settings.cat.terminal",
        "settings_cat_terminal",
        ("local_terminal",),
    ),
    (
        "settings.cat.behavior",
        "settings_cat_behavior",
        (
            "agent_backend",
            "auto_approve",
            "session_idle_hours",
            "haiku_naming",
            "language",
        ),
    ),
)


WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


_GROUP_TEXT_KEYS: dict[str, str] = {
    "settings_agent": "settings.agent.body",
    "settings_lag": "settings.lag.body",
    "settings_voice": "settings.voice.body",
    "settings_language": "settings.lang.body",
    "settings_weeklyday": "settings.weeklyday.body",
    "settings_approve": "settings.approve.body",
    "settings_local": "settings.local.body",
    "settings_cardhist": "settings.cardhist.body",
    "settings_pagesize": "settings.pagesize.body",
    "settings_screens": "settings.screens.body",
    "settings_cat_card": "settings.cat.card.body",
    "settings_cat_notifications": "settings.cat.notifications.body",
    "settings_cat_voice": "settings.cat.voice.body",
    "settings_cat_terminal": "settings.cat.terminal.body",
    "settings_cat_behavior": "settings.cat.behavior.body",
    "settings_bg_notify_finished": "settings.bg_notify.finished.body",
    "settings_bg_notify_error": "settings.bg_notify.error.body",
    "settings_bg_notify_needs_action": "settings.bg_notify.needs_action.body",
    "settings_haiku": "settings.haiku.body",
    "settings_idle_archive": "settings.idle_archive.body",
}
