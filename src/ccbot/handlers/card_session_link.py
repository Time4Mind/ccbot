"""Session-specific navigation for final answers that do not own live controls."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t
from ..session import session_manager
from .callback_data import CB_SW_USE


def session_link_keyboard(user_id: int, session_id: str) -> InlineKeyboardMarkup:
    sess = session_manager.get_session(session_id)
    name = getattr(sess, "name", "") or session_id
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    t(user_id, "btn.open_session", name=name),
                    callback_data=f"{CB_SW_USE}{session_id}",
                )
            ]
        ]
    )
