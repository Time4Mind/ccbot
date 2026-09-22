"""Node-scoped backend picker shared by new-session entry points."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..handlers.callback_data import CB_DIR_CANCEL, CB_NEW_BACKEND
from ..i18n import t
from ..session import session_manager


def build_backend_picker(
    user_id: int, *, node_id: str | None = None
) -> InlineKeyboardMarkup:
    node_id = node_id or session_manager.get_selected_node_id(user_id)
    enabled = session_manager.get_effective_backends(user_id, node_id)
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    name.capitalize(), callback_data=f"{CB_NEW_BACKEND}{name}"
                )
                for name in enabled
            ],
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_DIR_CANCEL)],
        ]
    )
