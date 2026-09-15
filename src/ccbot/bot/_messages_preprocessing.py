"""Pre-dispatch request preprocessing isolated from text routing."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from ..handlers.card_types import PendingPrompt
from ..handlers.notifications import (
    get_card_state,
    is_active_for_user,
    refresh_panel,
)
from ..request_preprocessing import prompt_preprocessor, should_preprocess
from ..session import session_manager

logger = logging.getLogger(__name__)


@dataclass
class PreparedDispatch:
    text: str
    record: dict[str, Any] | None = None
    session: Any = None
    preprocessed: bool = False

    def confirm_delivery(self) -> None:
        """Persist completion only after delivery to the pinned pane."""
        if self.record is None or self.session is None:
            return
        if self.preprocessed:
            self.session.remember_preprocessed_prompt(self.text)
        if self.record in self.session.pending_preprocessing:
            self.session.pending_preprocessing.remove(self.record)
        session_manager.save_state()


async def prepare_request_for_dispatch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    wid: str,
    text: str,
    *,
    input_kind: str,
) -> PreparedDispatch:
    """Rewrite an admitted request without ever re-resolving its target."""
    assert update.message is not None
    settings = session_manager.get_user_settings(user_id)
    if not should_preprocess(
        str(settings.get("preprocessing_mode", "off")), input_kind
    ):
        return PreparedDispatch(text=text)

    pinned_session = session_manager.find_session_by_window(wid)
    pending_prompt: PendingPrompt | None = None
    record: dict[str, Any] | None = None
    if pinned_session is not None:
        request_id = f"{user_id}:{update.message.message_id}"
        pinned_session.pending_preprocessing = [
            item
            for item in pinned_session.pending_preprocessing
            if item.get("request_id") != request_id
        ]
        record = {
            "request_id": request_id,
            "user_id": user_id,
            "original": text,
            "prepared": "",
            "instruction": str(settings.get("preprocessing_instruction", "")),
            "input_kind": input_kind,
            "state": "preprocessing",
            "preprocessed": False,
        }
        pinned_session.pending_preprocessing.append(record)
        session_manager.save_state()
        pending_prompt = PendingPrompt(
            request_id=str(update.message.message_id),
            text=text,
        )
        pending_state = get_card_state(user_id, pinned_session)
        pending_state.pending_prompts.append(pending_prompt)
        pending_state.current_page_idx = None
        if is_active_for_user(user_id, pinned_session):
            try:
                await refresh_panel(
                    context.bot,
                    user_id,
                    immediate=True,
                    refresh_pane=False,
                )
            except Exception as exc:
                logger.debug("preprocessing receipt repaint failed: %s", exc)

    was_preprocessed = False
    try:
        prepared = await prompt_preprocessor.process(
            text,
            instruction=str(settings.get("preprocessing_instruction", "")),
        )
    except Exception as exc:
        logger.warning(
            "request preprocessing failed user=%d window=%s kind=%s: %s",
            user_id,
            wid,
            input_kind,
            exc,
        )
    else:
        if prepared.strip():
            text = prepared.strip()
            was_preprocessed = True
            if pending_prompt is not None:
                pending_prompt.text = text
                pending_prompt.preprocessed = True
            if record is not None:
                record["prepared"] = text
                record["state"] = "dispatching"
                record["preprocessed"] = True
                session_manager.save_state()
            if pinned_session is not None and is_active_for_user(
                user_id, pinned_session
            ):
                try:
                    await refresh_panel(
                        context.bot,
                        user_id,
                        immediate=True,
                        refresh_pane=False,
                    )
                except Exception as exc:
                    logger.debug("preprocessed receipt repaint failed: %s", exc)

    if record is not None and not record["prepared"]:
        record["prepared"] = text
        record["state"] = "dispatching"
        session_manager.save_state()

    return PreparedDispatch(
        text=text,
        record=record,
        session=pinned_session,
        preprocessed=was_preprocessed,
    )


__all__ = ["PreparedDispatch", "prepare_request_for_dispatch"]
