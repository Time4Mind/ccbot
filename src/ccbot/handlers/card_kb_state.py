"""Enter and leave the interactive keyboard view on a live-card carrier."""

from __future__ import annotations

from __future__ import annotations

import logging
import time

from telegram import Bot

from ..session import Session, session_manager
from .card_model import (
    CardState,
)
from .kb_mode import build_kb_mode_keyboard

from .card_registry import (
    _cards,
    _card_lock,
    _legacy,
)

logger = logging.getLogger(__name__)


__all__ = [
    "_inline_screens_enabled",
    "has_pending_kb",
    "enter_kb_mode",
    "exit_kb_mode",
    "get_card_state",
]


def _inline_screens_enabled(user_id: int | None) -> bool:
    """Read the ``card_inline_screenshots`` user-setting (default False)."""
    if user_id is None:
        return False
    settings = session_manager.get_user_settings(user_id)
    return bool(settings.get("card_inline_screenshots", False))


def has_pending_kb(user_id: int, session_id: str) -> tuple[bool, bool]:
    """Return (has_prompt, in_kb_mode) for the (user, session) card.

    Public alternative to peeking at ``_cards``. ``has_prompt=True`` means
    a prompt is pending; ``in_kb_mode`` reflects whether the card msg is
    currently displaying kb-mode view vs the regular card.
    """
    state = _cards.get((user_id, session_id))
    if state is None:
        return False, False
    return bool(state.kb_prompt), state.in_kb_mode


async def enter_kb_mode(
    bot: Bot,
    user_id: int,
    sess: Session,
    prompt_content: str,
    ui_name: str,
) -> None:
    """Flip the active session's card msg into kb-mode view.

    Edits the existing card msg (or creates one if missing) so its body
    shows the prompt content and its keyboard is the kb-mode 3×3 grid +
    [Back][+ new][≡ Menu]. State is marked ``in_kb_mode=True`` and
    ``kb_prompt`` snapshot so subsequent paints stay consistent.

    No-op if state is already in kb-mode with the same prompt — avoids
    pointless edits when status_polling re-detects the prompt each poll.
    """
    state = get_card_state(user_id, sess)
    # Short-circuit ONLY when the kb-mode card is actually present in
    # chat. After ``close_card_view`` (Shot tap) ``msg_id`` is None but
    # ``in_kb_mode`` stays True — without the ``msg_id is not None``
    # check, subsequent status_polling re-detections of the same UI
    # would no-op and the kb-mode card would never be re-spawned.
    if (
        state.in_kb_mode
        and state.kb_prompt == prompt_content
        and state.msg_id is not None
    ):
        return
    state.kb_prompt = prompt_content
    state.kb_ui_name = ui_name
    state.in_kb_mode = True
    # kb-mode is an interrupt: claude is BLOCKED waiting for the user's
    # answer. If the user happens to be on Menu / List / Settings /
    # History on the same carrier (``in_menu_view=True``), ``_edit_card``
    # would short-circuit and the kb keyboard would never surface — the
    # user only saw it appear after tapping Shot, which dropped
    # ``msg_id=None`` and re-spawned a fresh card via ``_send_card``.
    # Clearing the flag here lets ``_edit_card`` repaint the carrier
    # with the kb prompt; the menu navigation is preempted because the
    # session can't proceed without the user's input anyway.
    state.in_menu_view = False
    if not sess.window_id:
        return
    text = _legacy("_render_card")(sess, state, user_id=user_id)
    kb = build_kb_mode_keyboard(user_id, sess.window_id, ui_name=ui_name)
    # Spawn-serialization (Task #50): a parallel ``update_session_card``
    # could otherwise observe ``msg_id is None`` during ``_send_card``
    # and spawn its own card too.
    async with _card_lock(user_id, sess.id):
        if state.msg_id is None:
            await _legacy("_send_card")(
                bot, user_id, sess, state, text=text, reply_markup=kb
            )
        else:
            await _legacy("_edit_card")(bot, user_id, state, text=text, reply_markup=kb)
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    logger.info(
        "kb_mode entered user=%d sess=%s ui=%s prompt_len=%d",
        user_id,
        sess.id,
        ui_name,
        len(prompt_content),
        extra={
            "event": "kb_mode_entered",
            "user_id": user_id,
            "session_id": sess.id,
            "ui_name": ui_name,
            "prompt_len": len(prompt_content),
        },
    )


async def exit_kb_mode(
    bot: Bot,
    user_id: int,
    sess: Session,
    *,
    clear_pending: bool = False,
) -> None:
    """Flip the card back from kb-mode to regular view.

    ``clear_pending=False`` (default) — user tapped Back. ``kb_prompt``
    is KEPT so the Resume button shows up in the footer. Tapping Resume
    re-enters kb-mode with the same prompt.

    ``clear_pending=True`` — claude moved past the prompt (terminal_parser
    no longer detects it, after double-poll confirm) OR user explicitly
    acted via a kb key. Wipe both ``in_kb_mode`` and ``kb_prompt`` so
    the Resume button disappears.
    """
    state = _cards.get((user_id, sess.id))
    if state is None:
        return
    was_in_kb = state.in_kb_mode
    state.in_kb_mode = False
    if clear_pending:
        state.kb_prompt = ""
        state.kb_ui_name = ""
    if state.msg_id is None or not was_in_kb:
        return
    text = _legacy("_render_card")(sess, state, user_id=user_id)
    if await _legacy("_edit_card")(bot, user_id, state, text=text):
        state.last_rendered = text
        state.last_edit_ts = time.monotonic()
    logger.info(
        "kb_mode exited user=%d sess=%s cleared=%s",
        user_id,
        sess.id,
        clear_pending,
        extra={
            "event": "kb_mode_exited",
            "user_id": user_id,
            "session_id": sess.id,
            "clear_pending": clear_pending,
        },
    )


def get_card_state(user_id: int, sess: Session) -> CardState:
    return _cards.setdefault((user_id, sess.id), CardState())
