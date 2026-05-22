"""Settings callbacks (CB_ST_*) — group navigation + per-value setters."""

from __future__ import annotations

import logging
from typing import Any, cast

from telegram.ext import ContextTypes

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ... import voice_install
from ...handlers.callback_data import (
    CB_ST_APPROVE,
    CB_ST_BACK,
    CB_ST_BGNOTIFY,
    CB_ST_CAT,
    CB_ST_CHIST,
    CB_ST_PAGESIZE,
    CB_ST_SCREENS,
    CB_ST_GRP,
    CB_ST_LAG,
    CB_ST_LANG,
    CB_ST_LCLAUDE,
    CB_ST_LOCAL,
    CB_ST_LTERM,
    CB_ST_PREV,
    CB_ST_VOICE,
    CB_ST_VOICE_INSTALL_GO,
    CB_ST_VOICE_INSTALL_NO,
    CB_ST_WDAY,
)
from ...handlers.menu import (
    Screen,
    build_footer_keyboard,
    render_more_text,
    render_settings_group_text,
    render_settings_text,
)
from ...handlers.message_sender import safe_edit, safe_send
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


async def _send_linux_claude_prompt(query: CallbackQuery, user_id: int) -> None:
    """Push a ready-to-paste prompt for the user to feed into a Claude session."""
    try:
        await safe_send(query.get_bot(), user_id, _CLAUDE_LINUX_PROMPT)
    except Exception as e:
        logger.debug("send_linux_claude_prompt failed: %s", e)


def _voice_value_needs_whisper(value: str) -> bool:
    """True when the chosen voice backend will dispatch to whisper.cpp.

    ``auto`` falls back to whisper.cpp on non-Darwin hosts, so the
    install prompt fires there too.
    """
    import sys

    if value == "whisper":
        return True
    if value == "auto" and sys.platform != "darwin":
        return True
    return False


_VOICE_INSTALL_HEAD = "🎙 *whisper.cpp* — авто-установка"


def _voice_install_prompt_text() -> str:
    plan = voice_install.describe_plan() or "(всё уже на месте — это странно)"
    return (
        f"{_VOICE_INSTALL_HEAD}\n\n"
        "На этом хосте не хватает компонентов для voice-backend "
        "`whisper`. Бот может поставить их сам:\n\n"
        f"{plan}\n\n"
        "Прогресс будет приходить отдельными сообщениями. Шаги "
        "выполняются под текущим пользователем (нужен root для "
        "`apt-get` / `/usr/local/bin`). Запустить установку?"
    )


def _voice_install_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Поставить", callback_data=CB_ST_VOICE_INSTALL_GO
                ),
                InlineKeyboardButton("✖ Отмена", callback_data=CB_ST_VOICE_INSTALL_NO),
            ]
        ]
    )


async def _maybe_offer_voice_install(
    query: CallbackQuery, user_id: int, value: str
) -> None:
    """Pop the OK/Cancel install prompt when the chosen backend will use
    whisper.cpp and the binary or model is missing on this host. No-op
    otherwise."""
    if not _voice_value_needs_whisper(value):
        return
    if voice_install.is_ready():
        return
    try:
        await safe_send(
            query.get_bot(),
            user_id,
            _voice_install_prompt_text(),
            reply_markup=_voice_install_keyboard(),
        )
    except Exception as e:
        logger.debug("voice install prompt send failed: %s", e)


# Module-level guard so a tap-spam on "Поставить" can't kick off
# two concurrent installs (each would race apt/cmake and corrupt the
# tree). One install at a time per user.
_install_inflight: set[int] = set()


async def _run_voice_install(query: CallbackQuery, user_id: int) -> None:
    """Drive the install steps, streaming progress as new chat messages."""
    if user_id in _install_inflight:
        try:
            await safe_send(
                query.get_bot(),
                user_id,
                "⏳ Установка уже идёт — жду текущую попытку.",
            )
        except Exception as e:
            logger.debug("voice install busy notice failed: %s", e)
        return
    _install_inflight.add(user_id)
    bot = query.get_bot()

    async def progress(text: str) -> None:
        try:
            await safe_send(bot, user_id, text)
        except Exception as e:
            logger.debug("voice install progress send failed: %s", e)

    try:
        ok = await voice_install.install_async(progress)
        logger.info(
            "voice_install finished user=%d ok=%s",
            user_id,
            ok,
            extra={
                "event": "voice_install_done",
                "user_id": user_id,
                "ok": ok,
            },
        )
    except Exception as e:
        logger.exception("voice install crashed: %s", e)
        try:
            await safe_send(bot, user_id, f"❌ Установка упала с исключением: `{e}`")
        except Exception:
            pass
    finally:
        _install_inflight.discard(user_id)


_GROUP_TO_SCREEN: dict[str, Screen] = {
    "language": "settings_language",
    "previews": "settings_previews",
    "live_lag": "settings_lag",
    "voice": "settings_voice",
    "weekly_reset_day": "settings_weeklyday",
    "auto_approve": "settings_approve",
    "local_terminal": "settings_local",
    "card_history": "settings_cardhist",
    "card_page_lines": "settings_pagesize",
    "card_inline_screenshots": "settings_screens",
}


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data == CB_ST_BACK:
        text = render_more_text(user.id)
        keyboard = build_footer_keyboard(user.id, screen="more")
        await safe_edit(query, text, reply_markup=keyboard)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    if data.startswith(CB_ST_GRP):
        group = data[len(CB_ST_GRP) :]
        grp_screen = _GROUP_TO_SCREEN.get(group)
        if not grp_screen:
            await query.answer("Unknown group")
            return True
        text = render_settings_group_text(user.id, grp_screen)
        keyboard = build_footer_keyboard(user.id, screen=grp_screen)
        await safe_edit(query, text, reply_markup=keyboard)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    if data.startswith(CB_ST_CAT):
        cat_screen = cast(Screen, data[len(CB_ST_CAT) :])
        if cat_screen == "settings":
            text = render_settings_text(user.id)
        else:
            text = render_settings_group_text(user.id, cat_screen)
        keyboard = build_footer_keyboard(user.id, screen=cat_screen)
        await safe_edit(query, text, reply_markup=keyboard)
        if query.message and keyboard is not None:
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        await query.answer()
        return True

    if data == CB_ST_LCLAUDE:
        await _send_linux_claude_prompt(query, user.id)
        await query.answer()
        return True

    if data == CB_ST_VOICE_INSTALL_NO:
        # Strip the kb from the prompt so the chat is tidy; leave the
        # body so the user can read what they declined.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug("voice install cancel: kb strip failed: %s", e)
        await query.answer("Отменено")
        return True

    if data == CB_ST_VOICE_INSTALL_GO:
        # Mark prompt as actioned (drop kb) and ack immediately — install
        # runs in the background and reports through progress messages.
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception as e:
            logger.debug("voice install go: kb strip failed: %s", e)
        await query.answer("Запускаю установку…")
        import asyncio as _asyncio

        _asyncio.create_task(_run_voice_install(query, user.id))
        return True

    setter_prefixes = (
        CB_ST_PREV,
        CB_ST_LAG,
        CB_ST_VOICE,
        CB_ST_LANG,
        CB_ST_WDAY,
        CB_ST_APPROVE,
        CB_ST_LOCAL,
        CB_ST_LTERM,
        CB_ST_CHIST,
        CB_ST_PAGESIZE,
        CB_ST_SCREENS,
        CB_ST_BGNOTIFY,
    )
    if not any(data.startswith(p) for p in setter_prefixes):
        return False

    screen_name: Screen = "settings"
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
            # When the user picks a backend that needs whisper.cpp and
            # the host doesn't have it, offer the auto-install. Fires
            # only on transition into that state — re-tapping the same
            # button re-offers, which is harmless and arguably useful
            # ("retry the install"). The prompt is a separate message
            # so it doesn't fight with the settings-screen carrier.
            await _maybe_offer_voice_install(query, user.id, value)
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
    elif data.startswith(CB_ST_CHIST):
        try:
            v = int(data[len(CB_ST_CHIST) :])
        except ValueError:
            v = 20
        if v in (10, 20, 50, 100):
            session_manager.update_user_setting(user.id, "card_history", v)
        screen_name = "settings_cardhist"
    elif data.startswith(CB_ST_PAGESIZE):
        try:
            v = int(data[len(CB_ST_PAGESIZE) :])
        except ValueError:
            v = 20
        if v in (10, 20, 40, 70):
            session_manager.update_user_setting(user.id, "card_page_lines", v)
        screen_name = "settings_pagesize"
    elif data.startswith(CB_ST_SCREENS):
        sval = data[len(CB_ST_SCREENS) :]
        if sval in ("on", "off"):
            new_val = sval == "on"
            session_manager.update_user_setting(
                user.id, "card_inline_screenshots", new_val
            )
            # Soft reset: nuke msg_id for all user's cards so the next
            # event creates a fresh msg of the correct type (photo+caption
            # vs text). Old artefacts stay in chat as frozen.
            from ...handlers.notifications import (
                reset_card_msg_id_for_user,
            )

            reset_card_msg_id_for_user(user.id)
        screen_name = "settings_screens"
    elif data.startswith(CB_ST_BGNOTIFY):
        payload = data[len(CB_ST_BGNOTIFY) :]
        try:
            key, sval = payload.split(":", 1)
        except ValueError:
            key, sval = "", ""
        if key in (
            "bg_notify_finished",
            "bg_notify_error",
            "bg_notify_needs_action",
        ) and sval in ("on", "off"):
            session_manager.update_user_setting(user.id, key, sval == "on")
        short = key.removeprefix("bg_notify_")
        screen_name = cast(Screen, f"settings_bg_notify_{short}")

    text = render_settings_group_text(user.id, screen_name)
    keyboard = build_footer_keyboard(user.id, screen=screen_name)
    await safe_edit(query, text, reply_markup=keyboard)
    await query.answer(t(user.id, "toast.saved"))
    return True
