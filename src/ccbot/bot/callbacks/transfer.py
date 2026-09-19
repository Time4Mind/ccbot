"""Interactive context-transfer flow: target node -> backend -> confirm."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from ...handlers.callback_data import (
    CB_FT_TRANSFER,
    CB_TR_BACK,
    CB_TR_BACKEND,
    CB_TR_CANCEL,
    CB_TR_CONFIRM,
    CB_TR_NODE,
)
from ...handlers.card_carrier import paint_card_on_carrier, pause_card_view
from ...handlers.card_pagination import _card_is_busy
from ...handlers.message_sender import safe_edit, safe_send
from ...handlers.notifications import get_card_state
from ...i18n import t
from ...session import session_manager
from ...session_import import build_full_import_context
from ...transfer_queue import (
    begin_transfer_queue,
    bind_transfer_queue,
    cancel_transfer_queue,
)
from ...transfer_runtime import get_node_runtime
from .._common import open_sessions_in_place, set_view

logger = logging.getLogger(__name__)

SOURCE_SESSION_KEY = "_transfer_source_session_id"
TARGET_NODE_KEY = "_transfer_target_node_id"
TARGET_BACKEND_KEY = "_transfer_target_backend"

_tasks: set[asyncio.Task[None]] = set()


def _available_backends(user_id: int, node: Any) -> tuple[str, ...]:
    if node.id == "local":
        return session_manager.get_enabled_backends(user_id)
    return tuple(dict.fromkeys(node.backends))


def _source_from_context(context: ContextTypes.DEFAULT_TYPE) -> Any | None:
    source_id = (context.user_data or {}).get(SOURCE_SESSION_KEY)
    return session_manager.get_session(source_id) if source_id else None


def _target_nodes(user_id: int, source: Any) -> list[Any]:
    return [
        node
        for node in session_manager.list_nodes()
        if node.id != source.node_id
        and node.state == "ready"
        and _available_backends(user_id, node)
    ]


def build_transfer_node_keyboard(user_id: int) -> InlineKeyboardMarkup:
    source = session_manager.get_active_session(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    if source is not None:
        for node in _target_nodes(user_id, source):
            rows.append(
                [
                    InlineKeyboardButton(
                        node.display_name or node.id,
                        callback_data=f"{CB_TR_NODE}{node.id}",
                    )
                ]
            )
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.cancel"), callback_data=CB_TR_CANCEL)]
    )
    return InlineKeyboardMarkup(rows)


def build_transfer_backend_keyboard(user_id: int, node_id: str) -> InlineKeyboardMarkup:
    node = session_manager.get_node(node_id)
    rows: list[list[InlineKeyboardButton]] = []
    if node is not None and node.state == "ready":
        for backend in _available_backends(user_id, node):
            rows.append(
                [
                    InlineKeyboardButton(
                        backend.capitalize(),
                        callback_data=f"{CB_TR_BACKEND}{node_id}:{backend}",
                    )
                ]
            )
    rows.extend(
        [
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_TR_BACK)],
            [
                InlineKeyboardButton(
                    t(user_id, "btn.cancel"), callback_data=CB_TR_CANCEL
                )
            ],
        ]
    )
    return InlineKeyboardMarkup(rows)


def _confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t(user_id, "transfer.btn.start"), callback_data=CB_TR_CONFIRM
                ),
                InlineKeyboardButton(
                    t(user_id, "btn.cancel"), callback_data=CB_TR_CANCEL
                ),
            ]
        ]
    )


def _set_flow_state(context: ContextTypes.DEFAULT_TYPE, **values: Any) -> None:
    if context.user_data is None:
        return
    context.user_data.update(values)


def _clear_flow_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data is None:
        return
    for key in (SOURCE_SESSION_KEY, TARGET_NODE_KEY, TARGET_BACKEND_KEY):
        context.user_data.pop(key, None)


async def _local_delivery(update: Any, context: Any, window_id: str) -> bool:
    """Route queued local prompts through the normal text handler."""
    if not getattr(update, "message", None) or not getattr(
        update.message, "text", None
    ):
        return False
    from .._messages_text import text_handler

    return await text_handler(update, context, pinned_wid=window_id)


async def _finish_transfer(
    *,
    user_id: int,
    bot: Any,
    query: CallbackQuery,
    transfer_id: str,
) -> None:
    transfer = session_manager.transfers.get(transfer_id)
    if transfer is None:
        return
    source = session_manager.get_session(transfer.source_session_id)
    runtime = get_node_runtime(transfer.target_node_id)
    if source is None or runtime is None:
        error = (
            "Target node runtime is not connected"
            if runtime is None
            else "Source session is no longer live"
        )
        session_manager.fail_context_transfer(transfer_id, error)
        cancel_transfer_queue(user_id)
        await safe_send(bot, user_id, t(user_id, "transfer.failed", error=error))
        return

    transfer.state = "starting"
    session_manager.save_state()
    try:
        if not transfer.context_path:
            context_path = await asyncio.to_thread(
                build_full_import_context, source, transfer.target_backend
            )
            transfer.context_path = str(context_path)
            session_manager.save_state()
        result = await runtime.start_context_transfer(
            transfer=transfer,
            source=source,
            user_id=user_id,
            bot=bot,
        )
        target = session_manager.complete_context_transfer(
            transfer_id,
            user_id=user_id,
            context_error=result.context_error,
            target_window_id=result.target_window_id,
            target_workdir=result.target_workdir,
            target_agent_session_id=result.target_agent_session_id,
            target_context_path=result.target_context_path,
        )
        delivery = result.delivery
        if delivery is None and target.node_id == "local" and target.window_id:

            async def local_delivery(update: Any, context: Any) -> bool:
                return await _local_delivery(update, context, target.window_id)

            delivery = local_delivery
        if delivery is not None:
            bind_transfer_queue(user_id, delivery)
        else:
            cancel_transfer_queue(user_id)
        if result.context_error:
            await safe_send(
                bot,
                user_id,
                t(
                    user_id,
                    "transfer.context_limit",
                    error=result.context_error,
                    path=target.context_path,
                ),
            )
        message = getattr(query, "message", None)
        if message is not None:
            await paint_card_on_carrier(bot, user_id, target, message.message_id)
        else:
            await safe_send(bot, user_id, t(user_id, "transfer.ready"))
    except Exception as exc:
        logger.exception("context transfer failed transfer=%s", transfer_id)
        session_manager.fail_context_transfer(transfer_id, str(exc))
        cancel_transfer_queue(user_id)
        await safe_send(bot, user_id, t(user_id, "transfer.failed", error=str(exc)))


def _schedule_transfer(
    *,
    user_id: int,
    bot: Any,
    query: CallbackQuery,
    transfer_id: str,
) -> None:
    task = asyncio.create_task(
        _finish_transfer(
            user_id=user_id,
            bot=bot,
            query=query,
            transfer_id=transfer_id,
        ),
        name=f"context-transfer:{transfer_id}",
    )
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def _node_picker_text(user_id: int, source: Any) -> str:
    return t(user_id, "transfer.choose_node", session=source.name or source.id)


async def handle(
    query: CallbackQuery, context: ContextTypes.DEFAULT_TYPE, user: Any
) -> bool:
    data = query.data or ""

    if data == CB_FT_TRANSFER:
        source = session_manager.get_active_session(user.id)
        settings = session_manager.get_user_settings(user.id)
        busy = source is not None and _card_is_busy(get_card_state(user.id, source))
        if (
            source is None
            or not session_manager.has_multiple_nodes
            or not settings.get("option_button_transfer", False)
            or busy
        ):
            await query.answer(t(user.id, "transfer.unavailable"), show_alert=False)
            return True
        _set_flow_state(
            context,
            **{
                SOURCE_SESSION_KEY: source.id,
                TARGET_NODE_KEY: None,
                TARGET_BACKEND_KEY: None,
            },
        )
        pause_card_view(user.id, source.id)
        await set_view(
            query,
            context.bot,
            user.id,
            _node_picker_text(user.id, source),
            build_transfer_node_keyboard(user.id),
        )
        await query.answer()
        return True

    if data == CB_TR_CANCEL:
        cancel_transfer_queue(user.id)
        _clear_flow_state(context)
        await open_sessions_in_place(query, context.bot, user.id)
        await query.answer(t(user.id, "transfer.cancelled"))
        return True

    if data == CB_TR_BACK:
        source = _source_from_context(context)
        if source is None:
            await query.answer(t(user.id, "transfer.unavailable"), show_alert=True)
            return True
        await set_view(
            query,
            context.bot,
            user.id,
            _node_picker_text(user.id, source),
            build_transfer_node_keyboard(user.id),
        )
        await query.answer()
        return True

    if data.startswith(CB_TR_NODE):
        source = _source_from_context(context)
        node_id = data[len(CB_TR_NODE) :]
        node = session_manager.get_node(node_id)
        if source is None or node is None or node not in _target_nodes(user.id, source):
            await query.answer(
                t(user.id, "transfer.target_unavailable"), show_alert=True
            )
            return True
        _set_flow_state(context, **{TARGET_NODE_KEY: node_id, TARGET_BACKEND_KEY: None})
        await set_view(
            query,
            context.bot,
            user.id,
            t(user.id, "transfer.choose_backend", node=node.display_name or node.id),
            build_transfer_backend_keyboard(user.id, node_id),
        )
        await query.answer()
        return True

    if data.startswith(CB_TR_BACKEND):
        raw = data[len(CB_TR_BACKEND) :]
        node_id, separator, backend = raw.rpartition(":")
        node = session_manager.get_node(node_id)
        if (
            not separator
            or node is None
            or node_id != (context.user_data or {}).get(TARGET_NODE_KEY)
            or backend not in _available_backends(user.id, node)
        ):
            await query.answer(
                t(user.id, "transfer.backend_unavailable"), show_alert=True
            )
            return True
        source = _source_from_context(context)
        if source is None:
            await query.answer(t(user.id, "transfer.unavailable"), show_alert=True)
            return True
        _set_flow_state(context, **{TARGET_BACKEND_KEY: backend})
        await set_view(
            query,
            context.bot,
            user.id,
            t(
                user.id,
                "transfer.confirm",
                session=source.name or source.id,
                node=node.display_name or node.id,
                backend=backend,
            ),
            _confirm_keyboard(user.id),
        )
        await query.answer()
        return True

    if data == CB_TR_CONFIRM:
        source = _source_from_context(context)
        target_node_id = (context.user_data or {}).get(TARGET_NODE_KEY)
        target_backend = (context.user_data or {}).get(TARGET_BACKEND_KEY)
        if source is None or not target_node_id or not target_backend:
            await query.answer(t(user.id, "transfer.unavailable"), show_alert=True)
            return True
        try:
            transfer = session_manager.start_context_transfer(
                source_session_id=source.id,
                target_node_id=target_node_id,
                target_backend=target_backend,
                context_path="",
            )
        except (OSError, ValueError, KeyError) as exc:
            await query.answer(str(exc), show_alert=True)
            return True
        begin_transfer_queue(user.id, transfer.id)
        _clear_flow_state(context)
        await safe_edit(query, t(user.id, "transfer.started"), reply_markup=None)
        await query.answer()
        _schedule_transfer(
            user_id=user.id,
            bot=context.bot,
            query=query,
            transfer_id=transfer.id,
        )
        return True

    return False


def reset_transfer_tasks_for_test() -> None:
    for task in tuple(_tasks):
        if not task.done():
            task.cancel()
    _tasks.clear()


__all__ = [
    "SOURCE_SESSION_KEY",
    "TARGET_BACKEND_KEY",
    "TARGET_NODE_KEY",
    "build_transfer_backend_keyboard",
    "build_transfer_node_keyboard",
    "handle",
    "reset_transfer_tasks_for_test",
]
