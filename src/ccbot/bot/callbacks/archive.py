"""Archive callbacks: cyclic pagination, restore, inspect and delete."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.archive import (
    DEFAULT_LOOKBACK_SECONDS,
    build_archive_page,
    restore_session,
)
from ...handlers.callback_data import (
    CB_ARC_BACK,
    CB_ARC_DELETE,
    CB_ARC_INSPECT,
    CB_ARC_PAGE,
    CB_ARC_RESTORE,
    CB_CONF_DEL_NO,
    CB_CONF_DEL_YES,
    CB_MM_BACK,
)
from ...handlers.history import render_archived_card_pages
from ...handlers.menu import build_footer_keyboard
from ...handlers.message_sender import safe_edit
from ...handlers.notifications import paint_card_on_carrier, reset_card
from ...i18n import t
from ...session import Session, session_manager
from .._common import render_session_preview

logger = logging.getLogger(__name__)
_restore_tasks: dict[tuple[int, str], asyncio.Task[None]] = {}


async def _restore_and_update(
    query: CallbackQuery,
    context: ContextTypes.DEFAULT_TYPE,
    user: Any,
    sess: Session,
) -> None:
    """Restore outside Telegram's callback deadline, then converge the surface."""
    try:
        ok, msg = await restore_session(context.bot, user.id, sess)
    except Exception as exc:
        logger.exception(
            "Archive restore task failed user_id=%s session_id=%s category=%s",
            user.id,
            sess.id,
            type(exc).__name__,
        )
        ok = False
        msg = str(exc).strip() or type(exc).__name__

    if ok:
        if query.message is not None:
            reset_card(user.id, sess.id)
            await paint_card_on_carrier(
                context.bot, user.id, sess, query.message.message_id
            )
            session_manager.set_last_switcher_msg(user.id, query.message.message_id)
        else:
            preview = await render_session_preview(sess)
            keyboard = build_footer_keyboard(user.id, screen="main")
            await safe_edit(query, preview, reply_markup=keyboard)
        return

    await safe_edit(query, t(user.id, "toast.restore_failed", msg=msg))


def _restore_done(key: tuple[int, str], task: asyncio.Task[None]) -> None:
    if _restore_tasks.get(key) is task:
        _restore_tasks.pop(key, None)
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Archive restore surface update failed user_id=%s session_id=%s",
            *key,
            exc_info=task.exception(),
        )


async def _build_inspect_text(sess: Session, user_id: int | None = None) -> str:
    """Render the body of the Archive → Inspect view.

    Surfaces the actual transcript (most recent page) via the live-card
    renderer, so thinking / tool bodies collapse into ``<details>``
    spoilers exactly like the active session card instead of dumping as
    an unreadable inline wall. Falls back to the short preview when there
    is no resolvable transcript (very old archives, glob miss, corrupt
    JSONL).

    Only the LAST page is shown — archived JSONLs can be huge and the
    Inspect keyboard carries no pagination controls. The header notes
    when older pages were truncated so the user knows to /restore for the
    full picture. ``user_id`` drives the per-user card line budget.
    """
    pages_total = await render_archived_card_pages(sess, user_id)
    if pages_total is None:
        text = await render_session_preview(sess)
    else:
        pages, total = pages_total
        text = pages[-1] if pages else ""
        if len(pages) > 1:
            prefix = f"_… {len(pages) - 1} older page(s) — restore to read fully ({total} events)_\n\n"
            text = prefix + text
    if sess.node_id != "local":
        node = session_manager.get_node(sess.node_id)
        node_name = node.display_name if node is not None else sess.node_id
        text = f"{t(user_id or 0, 'nodes.table.node')}: *{node_name}*\n\n{text}"
    return text


def _inspect_target(data: str) -> tuple[int, str]:
    """Parse new ``<page>:<sid>`` and legacy ``<sid>`` inspect payloads."""
    payload = data[len(CB_ARC_INSPECT) :]
    page_text, sep, sid = payload.partition(":")
    if not sep:
        return 0, payload
    try:
        page = max(0, int(page_text))
    except ValueError:
        return 0, payload
    return page, sid


def _back_page(data: str) -> int:
    """Parse ``ar:back:<page>``; old bare ``ar:back`` returns page zero."""
    suffix = data[len(CB_ARC_BACK) :]
    if not suffix.startswith(":"):
        return 0
    try:
        return max(0, int(suffix[1:]))
    except ValueError:
        return 0


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data.startswith(CB_ARC_PAGE):
        try:
            page = int(data[len(CB_ARC_PAGE) :])
        except ValueError:
            await query.answer(t(user.id, "toast.invalid_page"))
            return True
        text, keyboard = await build_archive_page(
            page=page,
            lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
            show_all=False,
            user_id=user.id,
            back_callback=CB_MM_BACK,
        )
        await safe_edit(query, text, reply_markup=keyboard)
        await query.answer()
        return True

    if data.startswith(CB_ARC_RESTORE):
        sid = data[len(CB_ARC_RESTORE) :]
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer(t(user.id, "toast.session_not_found"), show_alert=True)
            return True
        key = (user.id, sess.id)
        running = _restore_tasks.get(key)
        if running is not None and not running.done():
            await query.answer(t(user.id, "toast.restoring"))
            return True

        await query.answer(t(user.id, "toast.restoring"))
        task = asyncio.create_task(_restore_and_update(query, context, user, sess))
        _restore_tasks[key] = task
        task.add_done_callback(lambda done, task_key=key: _restore_done(task_key, done))
        return True

    if data.startswith(CB_ARC_INSPECT):
        origin_page, sid = _inspect_target(data)
        sess = session_manager.get_session(sid)
        if sess is None:
            await query.answer(t(user.id, "toast.session_not_found"), show_alert=True)
            return True
        text = await _build_inspect_text(sess, user.id)
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
                        t(user.id, "btn.back"),
                        callback_data=f"{CB_ARC_BACK}:{origin_page}",
                    ),
                ]
            ]
        )
        await safe_edit(query, text, reply_markup=kb)
        await query.answer()
        return True

    if data.startswith(CB_ARC_BACK):
        text, keyboard = await build_archive_page(
            page=_back_page(data),
            lookback_seconds=DEFAULT_LOOKBACK_SECONDS,
            show_all=False,
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
