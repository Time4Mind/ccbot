"""Settings callbacks (CB_ST_*) — group navigation + per-value setters."""

from __future__ import annotations

import logging
from typing import Any

from telegram.ext import ContextTypes

from ...config import config
from ...handlers.callback_data import (
    CB_ST_APPROVE,
    CB_ST_BACK,
    CB_ST_CPOS,
    CB_ST_GRP,
    CB_ST_LAG,
    CB_ST_LANG,
    CB_ST_LCLAUDE,
    CB_ST_LOCAL,
    CB_ST_LTERM,
    CB_ST_PREV,
    CB_ST_TOK,
    CB_ST_VOICE,
    CB_ST_WDAY,
)
from ...handlers.menu import (
    build_footer_keyboard,
    render_more_text,
    render_settings_group_text,
)
from ...handlers.message_sender import safe_send
from ...i18n import t
from ...session import session_manager

logger = logging.getLogger(__name__)


_CLAUDE_LINUX_PROMPT = """Help me wire ccbot's *Local terminal* feature on this Linux host.

The bot will run an external command of the form `<emulator> <args>...` \
where the placeholder `{shell}` is replaced (by ccbot, before exec) with \
a single shell-quoted argument that runs:

    tmux attach -t <session> \\\\; select-window -t @<wid> || true; exec bash -i

Step 1. Detect what's installed:
    which gnome-terminal kitty wezterm alacritty konsole tilix foot xterm

Step 2. If exactly one is available, pick it. Otherwise ask me which to use.

Step 3. Pick a template that opens a new tab when possible. Examples that \
work in practice:

    gnome-terminal -- bash -c {shell}
    konsole --new-tab -e bash -c {shell}
    kitty bash -c {shell}
    wezterm start -- bash -c {shell}
    alacritty -e bash -c {shell}

Step 4. Append (or update) this line in `~/.ccbot/.env`:

    CCBOT_LOCAL_TERMINAL_CMD=<your template, with {shell} verbatim>

Step 5. Restart ccbot (`./scripts/restart.sh`), then create a new session \
in the bot to verify a window pops up.

If something needs my input, use AskUserQuestion."""


async def _send_linux_claude_prompt(query: Any, user_id: int) -> None:
    """Push a ready-to-paste prompt for the user to feed into a Claude session."""
    try:
        await safe_send(query.get_bot(), user_id, _CLAUDE_LINUX_PROMPT)
    except Exception as e:
        logger.debug("send_linux_claude_prompt failed: %s", e)


_GROUP_TO_SCREEN = {
    "language": "settings_language",
    "previews": "settings_previews",
    "live_lag": "settings_lag",
    "voice": "settings_voice",
    "weekly_reset_day": "settings_weeklyday",
    "auto_approve": "settings_approve",
    "session_token_alerts": "settings_tokens",
    "local_terminal": "settings_local",
    "card_position": "settings_cardpos",
}


def _bump_token_threshold(user_id: int, slot: int, delta_sign: int) -> None:
    """Adjust slot ``slot`` of session_token_alerts by ±step, keep ascending."""
    if slot not in (0, 1, 2):
        return
    settings = session_manager.get_user_settings(user_id)
    raw = settings.get("session_token_alerts") or list(
        config.session_token_alert_defaults
    )
    if not isinstance(raw, list) or len(raw) != 3:
        raw = list(config.session_token_alert_defaults)
    values = [int(v) for v in raw]
    step = config.session_token_alert_step
    new_value = max(step, values[slot] + delta_sign * step)
    values[slot] = new_value
    # Re-sort to keep ascending order so adjacent slots stay valid.
    values.sort()
    session_manager.update_user_setting(user_id, "session_token_alerts", values)


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

    if data == CB_ST_LCLAUDE:
        await _send_linux_claude_prompt(query, user.id)
        await query.answer()
        return True

    setter_prefixes = (
        CB_ST_PREV,
        CB_ST_LAG,
        CB_ST_VOICE,
        CB_ST_LANG,
        CB_ST_WDAY,
        CB_ST_APPROVE,
        CB_ST_TOK,
        CB_ST_LOCAL,
        CB_ST_LTERM,
        CB_ST_CPOS,
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
    elif data.startswith(CB_ST_LOCAL):
        value = data[len(CB_ST_LOCAL) :]
        if value in ("off", "manual", "auto"):
            session_manager.update_user_setting(user.id, "local_terminal", value)
        screen_name = "settings_local"
    elif data.startswith(CB_ST_LTERM):
        from ...local_terminal import LINUX_TEMPLATES

        emu = data[len(CB_ST_LTERM) :]
        if emu in LINUX_TEMPLATES:
            session_manager.update_user_setting(
                user.id, "local_terminal_cmd", LINUX_TEMPLATES[emu]
            )
        screen_name = "settings_local"
    elif data.startswith(CB_ST_TOK):
        # Format: st:tok:<slot>:<+|->
        payload = data[len(CB_ST_TOK) :]
        try:
            slot_str, sign_str = payload.split(":", 1)
            slot = int(slot_str)
        except (ValueError, IndexError):
            await query.answer("Invalid")
            return True
        delta = 1 if sign_str == "+" else -1 if sign_str == "-" else 0
        if delta:
            _bump_token_threshold(user.id, slot, delta)
        screen_name = "settings_tokens"
    elif data.startswith(CB_ST_CPOS):
        value = data[len(CB_ST_CPOS) :]
        if value in ("push", "delete", "repost"):
            session_manager.update_user_setting(user.id, "card_position", value)
        screen_name = "settings_cardpos"

    text = render_settings_group_text(user.id, screen_name)  # type: ignore[arg-type]
    keyboard = build_footer_keyboard(user.id, screen=screen_name)  # type: ignore[arg-type]
    try:
        await query.edit_message_text(text=text, reply_markup=keyboard)
    except Exception as e:
        logger.debug("settings toggle edit failed: %s", e)
    await query.answer(t(user.id, "toast.saved"))
    return True
