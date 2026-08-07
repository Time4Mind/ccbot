"""Footer and Menu keyboard composition with compatibility re-exports.

Settings metadata, keyboard builders, and screen text live in leaf modules;
this stable module keeps the historical import surface and composes the final
inline keyboard attached to bot messages.
"""

from __future__ import annotations


from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import t
from ..session import session_manager
from .callback_data import (
    CB_FT_CLEAR,
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_STOP,
    CB_FT_TERM,
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_SHOT,
    CB_MM_STATUS,
    CB_PG_JUMP,
    CB_PG_NEXT,
    CB_PG_PREV,
    CB_SW_NEW,
    CB_SW_NOOP,
)
from .menu_settings import (
    _format_setting_value,
    _highlight,
    _parent_cat_cb,
    _settings_agent_grid,
    _settings_approve_grid,
    _settings_bg_notify_grid,
    _settings_cardhist_grid,
    _settings_category_grid,
    _settings_haiku_grid,
    _settings_idle_archive_grid,
    _settings_language_grid,
    _settings_lag_grid,
    _settings_local_grid,
    _settings_main_grid,
    _settings_pagesize_grid,
    _settings_screens_grid,
    _settings_voice_grid,
    _settings_weeklyday_grid,
)
from .menu_settings_data import (
    SETTINGS_CATEGORIES,
    WEEKDAYS,
    Screen,
    _GROUP_TEXT_KEYS,
    _SETTINGS_GROUPS,
)
from .menu_text import (
    render_more_text,
    render_settings_group_text,
    render_settings_text,
)
from .switcher import build_switcher_keyboard

__all__ = [
    "Screen",
    "_SETTINGS_GROUPS",
    "SETTINGS_CATEGORIES",
    "WEEKDAYS",
    "_has_active_session",
    "can_offer_terminal",
    "_has_pending_kb_action",
    "_footer_top_row",
    "_footer_bottom_row",
    "_MM_BUTTONS",
    "_more_grid",
    "_highlight",
    "_parent_cat_cb",
    "_format_setting_value",
    "_settings_main_grid",
    "_settings_category_grid",
    "_settings_lag_grid",
    "_settings_voice_grid",
    "_settings_language_grid",
    "_settings_agent_grid",
    "_settings_approve_grid",
    "_settings_idle_archive_grid",
    "_settings_local_grid",
    "_settings_cardhist_grid",
    "_settings_screens_grid",
    "_settings_haiku_grid",
    "_settings_bg_notify_grid",
    "_settings_pagesize_grid",
    "_settings_weeklyday_grid",
    "build_footer_keyboard",
    "render_settings_text",
    "_GROUP_TEXT_KEYS",
    "render_settings_group_text",
    "render_more_text",
]


def _has_active_session(user_id: int) -> bool:
    return session_manager.get_active_session(user_id) is not None


def can_offer_terminal(user_id: int) -> bool:
    """Show the "Open terminal" button on the live card for this user?

    Visible iff:
      * the user has an active session with a live window_id,
      * ``local_terminal`` is ``manual`` or ``auto`` (``off`` opts out
        of the feature entirely),
      * the platform can actually spawn a terminal (macOS always can;
        Linux needs a configured ``local_terminal_cmd`` whose emulator
        is on PATH — otherwise the click would silently no-op),
      * no tmux client is already attached to this window's group
        session (one is already enough).
    """
    import platform
    import shutil

    sess = session_manager.get_active_session(user_id)
    if sess is None or not sess.window_id:
        return False
    settings = session_manager.get_user_settings(user_id)
    mode = settings.get("local_terminal", "off")
    if mode not in ("manual", "auto"):
        return False
    system = platform.system()
    if system == "Linux":
        template = settings.get("local_terminal_cmd", "") or ""
        if not template:
            return False
        # Template's first word is the emulator binary.
        first = template.split(" ", 1)[0]
        if not first or shutil.which(first) is None:
            return False
    elif system != "Darwin":
        return False
    # Tmux side: hide the button when a terminal is already attached.
    from ..tmux_manager import tmux_manager

    try:
        if tmux_manager.has_client_for_window(sess.window_id):
            return False
    except Exception:
        # Defensive: if the probe fails, fall through to "offer button" —
        # better one stray button than the user having no way to reopen.
        pass
    return True


def _has_pending_kb_action(user_id: int) -> bool:
    """True when the user's active session has an unresolved kb-mode
    prompt — needs_action detected but user hasn't acted on it. Shows
    the [🔙 Resume action] button in place of Shot in the footer.
    """
    from .notifications import has_pending_kb

    active = session_manager.get_active_session(user_id)
    if active is None:
        return False
    has_prompt, in_kb = has_pending_kb(user_id, active.id)
    return has_prompt and not in_kb


def _footer_top_row(
    user_id: int, *, is_busy: bool = True
) -> list[InlineKeyboardButton]:
    """Default top row — per-session controls. Menu lives in its own
    bottom row (see ``_footer_bottom_row``) so its slot stays put
    across view transitions (the same spot Back occupies in
    /archive / settings sub-screens).

    Busy session shows *Stop* (sends Escape — interrupt the running
    task without terminating). Idle session shows *Kill* (archive the
    whole session). The is_busy signal comes from
    ``notifications._card_is_busy`` which keys off "card is alive" so
    the button doesn't flicker between tool calls.

    When the active session has an unresolved kb-mode prompt (user
    pressed Back from kb-mode but pending still active), Shot is
    replaced with [🔙 Resume action] so the user can re-enter kb-mode.
    """
    from .callback_data import CB_KB_RESUME

    row: list[InlineKeyboardButton] = []
    if _has_active_session(user_id):
        if is_busy:
            row.append(
                InlineKeyboardButton(t(user_id, "btn.stop"), callback_data=CB_FT_STOP)
            )
        else:
            row.append(
                InlineKeyboardButton(t(user_id, "btn.kill"), callback_data=CB_FT_KILL)
            )
        row.append(
            InlineKeyboardButton(t(user_id, "btn.clear"), callback_data=CB_FT_CLEAR)
        )
        # Pending kb action → Resume on Shot slot. Otherwise Shot as usual.
        if _has_pending_kb_action(user_id):
            row.append(
                InlineKeyboardButton("🔙 Resume action", callback_data=CB_KB_RESUME)
            )
        else:
            row.append(
                InlineKeyboardButton(t(user_id, "mm.shot"), callback_data=CB_MM_SHOT)
            )
        # Open-terminal sits with the other per-session controls so the
        # button persists across switcher taps (which re-render the
        # footer top row for the newly-active session). Visible only
        # when ``local_terminal`` ∈ {manual, auto} AND no tmux client
        # is currently attached to this session's window group.
        if can_offer_terminal(user_id):
            row.append(
                InlineKeyboardButton(t(user_id, "btn.term"), callback_data=CB_FT_TERM)
            )
    return row


def _footer_bottom_row(user_id: int) -> list[InlineKeyboardButton]:
    """Bottom row for the main screen: `[+ new] [≡ Menu]`. The pair sits
    on a single row so the two most-used "go elsewhere" affordances land
    side-by-side and the user's eye doesn't ping-pong between rows. Same
    slot as Back on /archive / settings sub-screens, just with
    two buttons instead of one.
    """
    return [
        InlineKeyboardButton("+ new", callback_data=CB_SW_NEW),
        InlineKeyboardButton(t(user_id, "btn.menu"), callback_data=CB_FT_MORE),
    ]


_MM_BUTTONS: tuple[tuple[str, str, str], ...] = (
    ("sessions", "mm.sessions", CB_MM_LIST),
    ("archive", "mm.archive", CB_MM_ARCHIVE),
    ("status", "mm.status", CB_MM_STATUS),
    ("new", "mm.new", CB_MM_NEW),
    ("settings", "mm.settings", CB_MM_SETTINGS),
)


def _more_grid(
    user_id: int, *, exclude: str | None = None
) -> list[list[InlineKeyboardButton]]:
    """The expanded Menu screen — replaces the default top row.

    `exclude` removes the named button (e.g. "status") so a sub-screen
    that opened via that button doesn't show a self-link, AND surfaces a
    Back row that returns to Menu. The Menu top-level (exclude=None) is the
    home screen — no Back row, since there is no parent.
    """
    buttons = [
        InlineKeyboardButton(t(user_id, label_key), callback_data=cb)
        for key, label_key, cb in _MM_BUTTONS
        if key != exclude
    ]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    if exclude is not None:
        rows.append(
            [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_BACK)]
        )
    # Menu top-level intentionally has no "Close" — typing in chat auto-
    # resumes the live card via ``resume_card_view`` in text_handler, and
    # adding an explicit button confused users in the no-active-card case
    # where it would silently no-op. Section buttons themselves provide
    # navigation; a no-op escape isn't worth the extra row.
    return rows


def build_footer_keyboard(
    user_id: int,
    *,
    screen: Screen = "main",
    include_lost_in_switcher: bool = False,
    is_busy: bool = True,
    exclude_more: str | None = None,
    include_older_btn: bool = True,
) -> InlineKeyboardMarkup | None:
    """Compose footer rows + switcher row for the requested screen.

    Returns None only when there is genuinely nothing to render (no sessions
    AND no actionable footer button) — currently this never happens because
    the More button is always available.
    """
    rows: list[list[InlineKeyboardButton]] = []

    is_settings = screen.startswith("settings")
    is_more_view = screen == "more" or exclude_more is not None
    # On Menu and its sub-screens we already show 🆕 New explicitly in the
    # grid — don't duplicate "+ new" inside the switcher row, and drop the
    # active-session no-op button (it does nothing on tap).
    drop_active_from_switcher = is_more_view
    # Settings is a configuration surface; the switcher carries no useful
    # action there (active button is a no-op).
    #
    # Menu (``screen == "more"``) drops the switcher too — session-switch
    # buttons live in Menu → List from now on (per user request). The Menu
    # grid offers explicit access via the List entry.
    include_switcher = not is_settings and screen != "more"
    # The main screen now anchors `+ new` next to ≡ Menu in the bottom
    # row instead of carrying it inside the switcher (so the two go-
    # elsewhere affordances sit side-by-side). Sub-screens with
    # ``exclude_more`` (e.g. history view) own their own Back row and
    # don't want a stray `+ new` either.
    include_new_in_switcher = not is_more_view and screen != "main"

    if screen == "more":
        rows.extend(_more_grid(user_id, exclude=exclude_more))
    elif screen == "settings":
        rows.extend(_settings_main_grid(user_id))
    elif screen == "settings_lag":
        rows.extend(_settings_lag_grid(user_id))
    elif screen == "settings_voice":
        rows.extend(_settings_voice_grid(user_id))
    elif screen == "settings_language":
        rows.extend(_settings_language_grid(user_id))
    elif screen == "settings_agent":
        rows.extend(_settings_agent_grid(user_id))
    elif screen == "settings_weeklyday":
        rows.extend(_settings_weeklyday_grid(user_id))
    elif screen == "settings_approve":
        rows.extend(_settings_approve_grid(user_id))
    elif screen == "settings_idle_archive":
        rows.extend(_settings_idle_archive_grid(user_id))
    elif screen == "settings_local":
        rows.extend(_settings_local_grid(user_id))
    elif screen == "settings_cardhist":
        rows.extend(_settings_cardhist_grid(user_id))
    elif screen == "settings_pagesize":
        rows.extend(_settings_pagesize_grid(user_id))
    elif screen == "settings_screens":
        rows.extend(_settings_screens_grid(user_id))
    elif screen in (
        "settings_cat_card",
        "settings_cat_notifications",
        "settings_cat_voice",
        "settings_cat_terminal",
        "settings_cat_behavior",
    ):
        rows.extend(_settings_category_grid(user_id, screen))
    elif screen == "settings_bg_notify_finished":
        rows.extend(
            _settings_bg_notify_grid(
                user_id, "bg_notify_finished", "settings_cat_notifications"
            )
        )
    elif screen == "settings_bg_notify_error":
        rows.extend(
            _settings_bg_notify_grid(
                user_id, "bg_notify_error", "settings_cat_notifications"
            )
        )
    elif screen == "settings_bg_notify_needs_action":
        rows.extend(
            _settings_bg_notify_grid(
                user_id, "bg_notify_needs_action", "settings_cat_notifications"
            )
        )
    elif screen == "settings_haiku":
        rows.extend(_settings_haiku_grid(user_id))
    else:
        # In-card pagination row at the very top — [◀] [N/M] [▶].
        # ``N/M`` taps jump to the default-focus page (latest answer).
        # ◀ at page 0 falls back to opening the older-history view
        # (the full transcript, beyond the card's CARD_MAX_EVENTS).
        # Suppressed in callers that compose this keyboard as extras
        # BELOW a history-view's own pagination row.
        if include_older_btn and _has_active_session(user_id):
            active = session_manager.get_active_session(user_id)
            page_idx = 0
            total_pages = 1
            if active is not None:
                from .notifications import card_page_info, get_card_state

                page_idx, total_pages = card_page_info(
                    get_card_state(user_id, active), user_id
                )
            pag_row: list[InlineKeyboardButton] = [
                InlineKeyboardButton("◀", callback_data=CB_PG_PREV),
                InlineKeyboardButton(
                    f"{page_idx + 1}/{total_pages}", callback_data=CB_PG_JUMP
                ),
                InlineKeyboardButton("▶", callback_data=CB_PG_NEXT),
            ]
            rows.append(pag_row)
        top = _footer_top_row(user_id, is_busy=is_busy)
        if top:
            rows.append(top)

    if include_switcher:
        sw = build_switcher_keyboard(
            user_id,
            include_lost=include_lost_in_switcher,
            include_new=include_new_in_switcher,
        )
        if sw is not None:
            for sw_row in sw.inline_keyboard:
                row_list = list(sw_row)
                if drop_active_from_switcher:
                    row_list = [
                        b for b in row_list if (b.callback_data or "") != CB_SW_NOOP
                    ]
                if row_list:
                    rows.append(row_list)

    # Main / live-card view: anchor ⋯ Menu at the very bottom so its
    # position matches Back / Close in the menu sub-screens. Sub-screens
    # add their own Back row inside their grid builders.
    if screen == "main":
        rows.append(_footer_bottom_row(user_id))

    if not rows:
        return None
    return InlineKeyboardMarkup(rows)
