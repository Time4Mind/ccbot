"""Archive callbacks (CB_ARC_*).

Pagination, restore, inspect, delete-confirmation, and the 72h ↔ 14d window toggle.
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...config import config
from ...handlers.archive import (
    DEFAULT_LOOKBACK_SECONDS,
    build_archive_page,
    restore_session,
)
from ...handlers.callback_data import (
    CB_ARC_ALL,
    CB_ARC_BACK,
    CB_ARC_DELETE,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
    CB_ARC_RESTORE,
    CB_CONF_DEL_NO,
    CB_CONF_DEL_YES,
    CB_MM_BACK,
)
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_edit
from ...i18n import t
from ...session import session_manager
from .._common import render_session_preview

logger = logging.getLogger(__name__)


def _show_all(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data and context.user_data.get("_arc_show_all", False))


def _lookback(show_all: bool) -> float:
    return config.archive_purge_after if show_all else DEFAULT_LOOKBACK_SECONDS


async def handle(query: Any, context: ContextTypes.DEFAULT_TYPE, user: Any) -> bool:
    data = query.data or ""

    if data.startswith(CB_ARC_PAGE):
        try:
            page = int(data[len(CB_ARC_PAGE) :])
        except ValueError:
            await query.answer(t(user.id, "toast.invalid_page"))
            return True
        show_all = _show_all(context)
        text, keyboard = build_archive_page(
            page=page,
            lookback_seconds=_lookback(show_all),
            show_all=show_all,
            user_id=user.id,
            back_callback=CB_MM_BACK,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return True

    if data == CB_ARC_ALL:
        new = not _show_all(context)
        if context.user_data is not None:
            context.user_data["_arc_show_all"] = new
        text, keyboard = build_archive_page(
            page=0,
            lookback_seconds=_lookback(new),
            show_all=new,
            user_id=user.id,
            back_callback=CB_MM_BACK,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer(t(user.id, "toast.range_14d" if new else "toast.range_72h"))
        return True

    if data.startswith(CB_ARC_RESTORE):
        sid = data[len(CB_ARC_RESTORE) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer(t(user.id, "toast.session_not_found"), show_alert=True)
            return True
        ok, msg = await restore_session(context.bot, user.id, sess)
        if ok:
            preview = await render_session_preview(sess)
            keyboard = build_footer_keyboard(user.id, screen="main")
            await safe_edit(query, preview, reply_markup=keyboard)
            if query.message and keyboard is not None:
                session_manager.set_last_switcher_msg(user.id, query.message.message_id)
            await query.answer(t(user.id, "toast.restored"))
        else:
            await query.answer(
                t(user.id, "toast.restore_failed", msg=msg), show_alert=True
            )
        return True

    if data.startswith(CB_ARC_INSPECT):
        sid = data[len(CB_ARC_INSPECT) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer(t(user.id, "toast.session_not_found"), show_alert=True)
            return True
        preview = await render_session_preview(sess)
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user.id, "btn.restore"),
                        callback_data=f"{CB_ARC_RESTORE}{sess.id}"[:64],
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.delete"),
                        callback_data=f"{CB_ARC_DELETE}{sess.id}"[:64],
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.back"), callback_data=CB_ARC_BACK
                    ),
                ]
            ]
        )
        await safe_edit(query, preview, reply_markup=kb)
        await query.answer()
        return True

    if data == CB_ARC_BACK:
        show_all = _show_all(context)
        text, keyboard = build_archive_page(
            page=0,
            lookback_seconds=_lookback(show_all),
            show_all=show_all,
            user_id=user.id,
            back_callback=CB_MM_BACK,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return True

    if data.startswith(CB_ARC_DELETE):
        sid = data[len(CB_ARC_DELETE) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer(t(user.id, "toast.already_gone"), show_alert=False)
            return True
        kb = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        t(user.id, "btn.yes_delete"),
                        callback_data=f"{CB_CONF_DEL_YES}{sess.id}"[:64],
                    ),
                    InlineKeyboardButton(
                        t(user.id, "btn.no"), callback_data=CB_CONF_DEL_NO
                    ),
                ]
            ]
        )
        await safe_edit(
            query, t(user.id, "conf.delete", name=sess.name), reply_markup=kb
        )
        await query.answer()
        return True

    return False
