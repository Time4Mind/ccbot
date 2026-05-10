"""Settings callbacks (CB_ST_*) — group navigation + per-value setters."""

from __future__ import annotations

import logging
from typing import Any

from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_ST_APPROVE,
    CB_ST_BACK,
    CB_ST_GRP,
    CB_ST_LAG,
    CB_ST_LANG,
    CB_ST_PREV,
    CB_ST_VOICE,
    CB_ST_WDAY,
)
from ...handlers.menu import (
    build_footer_keyboard,
    render_more_text,
    render_settings_group_text,
)
from ...i18n import t
from ...session import session_manager

logger = logging.getLogger(__name__)


_GROUP_TO_SCREEN = {
    "language": "settings_language",
    "previews": "settings_previews",
    "live_lag": "settings_lag",
    "voice": "settings_voice",
    "weekly_reset_day": "settings_weeklyday",
    "auto_approve": "settings_approve",
}


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data == CB_ST_BACK:
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("settings back edit failed: %s", e)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    if data.startswith(CB_ST_GRP):
        group = data[len(CB_ST_GRP) :]
        screen_name = _GROUP_TO_SCREEN.get(group)
        if not screen_name:
            await query.answer("Unknown group")
            return True
        text = render_settings_group_text(user.id, screen_name)  # type: ignore[arg-type]
        keyboard = build_footer_keyboard(user.id, screen=screen_name)  # type: ignore[arg-type]
        try:
            await query.edit_message_text(text=text, reply_markup=keyboard)
        except Exception as e:
            logger.debug("settings group open failed: %s", e)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    setter_prefixes = (
        CB_ST_PREV,
        CB_ST_LAG,
        CB_ST_VOICE,
        CB_ST_LANG,
        CB_ST_WDAY,
        CB_ST_APPROVE,
    )
    if not any(data.startswith(p) for p in setter_prefixes):
        return False

    screen_name = "settings"
    if data.startswith(CB_ST_PREV):
        value = data[len(CB_ST_PREV) :]
        if value in ("economical", "readable"):
            session_manager.update_user_setting(user.id, "previews", value)
        screen_name = "settings_previews"
    elif data.startswith(CB_ST_LAG):
        try:
            lag = int(data[len(CB_ST_LAG) :])
        except ValueError:
            lag = 4
        if lag in (0, 2, 4, 8):
            session_manager.update_user_setting(user.id, "live_lag", lag)
        screen_name = "settings_lag"
    elif data.startswith(CB_ST_VOICE):
        value = data[len(CB_ST_VOICE) :]
        if value in ("auto", "whisper", "apple", "off"):
            session_manager.update_user_setting(user.id, "voice", value)
        screen_name = "settings_voice"
    elif data.startswith(CB_ST_LANG):
        value = data[len(CB_ST_LANG) :]
        if value in ("en", "ru", "zh"):
            session_manager.update_user_setting(user.id, "language", value)
        screen_name = "settings_language"
    elif data.startswith(CB_ST_WDAY):
        value = data[len(CB_ST_WDAY) :]
        if value in ("mon", "tue", "wed", "thu", "fri", "sat", "sun"):
            session_manager.update_user_setting(user.id, "weekly_reset_day", value)
        screen_name = "settings_weeklyday"
    elif data.startswith(CB_ST_APPROVE):
        value = data[len(CB_ST_APPROVE) :]
        if value in ("off", "on"):
            session_manager.update_user_setting(user.id, "auto_approve", value)
        screen_name = "settings_approve"

    text = render_settings_group_text(user.id, screen_name)  # type: ignore[arg-type]
    keyboard = build_footer_keyboard(user.id, screen=screen_name)  # type: ignore[arg-type]
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard)
    except Exception as e:
        logger.debug("settings toggle edit failed: %s", e)
    await query.answer(t(user.id, "toast.saved"))
    return True
