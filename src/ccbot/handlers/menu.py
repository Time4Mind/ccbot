"""Footer + More menu + Settings — inline keyboards under the last bot message.

Three layers, all rendered together onto the same message:

  - Top row:    Stop (only when an active session exists) + ⋯ More
  - Optional:   More menu grid (List / Status / History / Shot / New / ⚙)
  - Optional:   Settings toggles (when user is inside ⚙)
  - Bottom row: A8 session switcher (`+ new`)

`build_footer_keyboard(user_id, screen=...)` returns the right combination
based on which "screen" the user is currently viewing.

Public API:
  build_footer_keyboard(user_id, screen) -> InlineKeyboardMarkup | None
  build_more_keyboard(user_id) -> InlineKeyboardMarkup
  build_settings_keyboard(user_id) -> InlineKeyboardMarkup
"""

from __future__ import annotations

from typing import Literal

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import LANGUAGES, t
from ..session import session_manager
from .callback_data import (
    CB_FT_CLEAR,
    CB_FT_KILL,
    CB_FT_MORE,
    CB_FT_STOP,
    CB_FT_TERM,
    CB_PG_JUMP,
    CB_PG_NEXT,
    CB_PG_PREV,
    CB_MM_ARCHIVE,
    CB_MM_BACK,
    CB_MM_LIST,
    CB_MM_NEW,
    CB_MM_SETTINGS,
    CB_MM_SHOT,
    CB_MM_STATUS,
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
    CB_ST_VOICE,
    CB_ST_WDAY,
    CB_SW_NEW,
    CB_SW_NOOP,
)
from .switcher import build_switcher_keyboard

Screen = Literal[
    "main",
    "more",
    "settings",
    "settings_previews",
    "settings_lag",
    "settings_voice",
    "settings_language",
    "settings_weeklyday",
    "settings_approve",
    "settings_local",
    "settings_cardpos",
]

# Group key -> (label translation key, sub-screen name, settings-dict key)
_SETTINGS_GROUPS: tuple[tuple[str, str, str, str], ...] = (
    ("language", "settings.group.language", "settings_language", "language"),
    ("previews", "settings.group.previews", "settings_previews", "previews"),
    ("live_lag", "settings.group.live_lag", "settings_lag", "live_lag"),
    ("voice", "settings.group.voice", "settings_voice", "voice"),
    (
        "weekly_reset_day",
        "settings.group.weekly_reset_day",
        "settings_weeklyday",
        "weekly_reset_day",
    ),
    (
        "auto_approve",
        "settings.group.auto_approve",
        "settings_approve",
        "auto_approve",
    ),
    (
        "local_terminal",
        "settings.group.local_terminal",
        "settings_local",
        "local_terminal",
    ),
    (
        "card_position",
        "settings.group.card_position",
        "settings_cardpos",
        "card_position",
    ),
)

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _has_active_session(user_id: int) -> bool:
    return session_manager.get_active_session(user_id) is not None


def can_offer_terminal(user_id: int) -> bool:
    """Show the "Open terminal" button (in /list) for this user?

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


def _footer_top_row(
    user_id: int, *, is_busy: bool = True
) -> list[InlineKeyboardButton]:
    """Default top row — per-session controls. Menu lives in its own
    bottom row (see ``_footer_bottom_row``) so its slot stays put
    across view transitions (the same spot Back occupies in
    /list / /archive / other sub-screens).

    Busy session shows *Stop* (sends Escape — interrupt the running
    task without terminating). Idle session shows *Kill* (archive the
    whole session). The is_busy signal comes from
    ``notifications._card_is_busy`` which keys off "card is alive" so
    the button doesn't flicker between tool calls.
    """
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
        # Shot belongs next to Kill / Clear — it's a per-session control
        # too ("snapshot this session's terminal"). Keeping it in the
        # top row means the screenshot button stays put across switcher
        # taps, /list paints, and live-card edits (anywhere the main
        # screen is rendered).
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
    slot as Back on /list / /archive / settings sub-screens, just with
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


def _highlight(label: str, active: bool) -> str:
    return f"• {label}" if active else label


def _settings_main_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    """Top-level Settings screen: one button per group + Back-to-Menu."""
    s = session_manager.get_user_settings(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    for key, label_key, _screen, value_key in _SETTINGS_GROUPS:
        cur = s.get(value_key, "")
        label = t(user_id, label_key)
        if value_key == "live_lag":
            value_str = f"{int(cur)}s"
        elif value_key == "weekly_reset_day":
            value_str = t(user_id, f"day.{cur}") if cur else "?"
        elif value_key == "auto_approve":
            value_str = t(user_id, f"approve.{cur}") if cur else "?"
        elif value_key == "local_terminal":
            value_str = t(user_id, f"local.{cur}") if cur else "?"
        elif value_key == "card_position":
            value_str = t(user_id, f"cardpos.{cur}") if cur else "?"
        else:
            value_str = str(cur)
        rows.append(
            [
                InlineKeyboardButton(
                    f"{label}: {value_str}",
                    callback_data=f"{CB_ST_GRP}{key}",
                )
            ]
        )
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_ST_BACK)]
    )
    return rows


def _settings_previews_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("previews", "economical")
    return [
        [
            InlineKeyboardButton(
                _highlight("economical", cur == "economical"),
                callback_data=f"{CB_ST_PREV}economical",
            ),
            InlineKeyboardButton(
                _highlight("readable", cur == "readable"),
                callback_data=f"{CB_ST_PREV}readable",
            ),
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_lag_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = int(session_manager.get_user_settings(user_id).get("live_lag", 4))
    return [
        [
            InlineKeyboardButton(
                _highlight(f"{v}s", cur == v),
                callback_data=f"{CB_ST_LAG}{v}",
            )
            for v in (0, 2, 4, 8)
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_voice_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("voice", "auto")
    return [
        [
            InlineKeyboardButton(
                _highlight(v, cur == v),
                callback_data=f"{CB_ST_VOICE}{v}",
            )
            for v in ("auto", "whisper", "apple", "off")
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_language_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("language", "en")
    return [
        [
            InlineKeyboardButton(
                _highlight(f"{label}", cur == code),
                callback_data=f"{CB_ST_LANG}{code}",
            )
            for code, label in LANGUAGES
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_approve_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("auto_approve", "off")
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"approve.{v}"), cur == v),
                callback_data=f"{CB_ST_APPROVE}{v}",
            )
            for v in ("off", "on")
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_local_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    import platform

    from ..local_terminal import LINUX_TEMPLATES, detect_linux_emulators

    settings = session_manager.get_user_settings(user_id)
    cur = settings.get("local_terminal", "off")
    cur_cmd = settings.get("local_terminal_cmd", "")

    rows: list[list[InlineKeyboardButton]] = []
    rows.append(
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"local.{v}"), cur == v),
                callback_data=f"{CB_ST_LOCAL}{v}",
            )
            for v in ("off", "manual", "auto")
        ]
    )

    # Linux + a terminal-enabled mode: surface the emulator picker.
    # Empty list → fall back to the claude-typed snippet flow.
    if cur in ("manual", "auto") and platform.system() == "Linux":
        detected = detect_linux_emulators()
        if detected:
            for i in range(0, len(detected), 2):
                row: list[InlineKeyboardButton] = []
                for name in detected[i : i + 2]:
                    selected = cur_cmd == LINUX_TEMPLATES[name]
                    row.append(
                        InlineKeyboardButton(
                            _highlight(name, selected),
                            callback_data=f"{CB_ST_LTERM}{name}",
                        )
                    )
                rows.append(row)
        rows.append(
            [
                InlineKeyboardButton(
                    t(user_id, "settings.local.claude_help"),
                    callback_data=CB_ST_LCLAUDE,
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)]
    )
    return rows


def _settings_cardpos_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("card_position", "push")
    return [
        [
            InlineKeyboardButton(
                _highlight(t(user_id, f"cardpos.{v}"), cur == v),
                callback_data=f"{CB_ST_CPOS}{v}",
            )
            for v in ("push", "delete", "repost")
        ],
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)],
    ]


def _settings_weeklyday_grid(user_id: int) -> list[list[InlineKeyboardButton]]:
    cur = session_manager.get_user_settings(user_id).get("weekly_reset_day", "mon")
    rows: list[list[InlineKeyboardButton]] = []
    # 4 + 3 layout fits comfortably on a phone.
    week = list(WEEKDAYS)
    for chunk_start in (0, 4):
        chunk = week[chunk_start : chunk_start + 4]
        rows.append(
            [
                InlineKeyboardButton(
                    _highlight(t(user_id, f"day.{d}"), cur == d),
                    callback_data=f"{CB_ST_WDAY}{d}",
                )
                for d in chunk
            ]
        )
    rows.append(
        [InlineKeyboardButton(t(user_id, "btn.back"), callback_data=CB_MM_SETTINGS)]
    )
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
    elif screen == "settings_previews":
        rows.extend(_settings_previews_grid(user_id))
    elif screen == "settings_lag":
        rows.extend(_settings_lag_grid(user_id))
    elif screen == "settings_voice":
        rows.extend(_settings_voice_grid(user_id))
    elif screen == "settings_language":
        rows.extend(_settings_language_grid(user_id))
    elif screen == "settings_weeklyday":
        rows.extend(_settings_weeklyday_grid(user_id))
    elif screen == "settings_approve":
        rows.extend(_settings_approve_grid(user_id))
    elif screen == "settings_local":
        rows.extend(_settings_local_grid(user_id))
    elif screen == "settings_cardpos":
        rows.extend(_settings_cardpos_grid(user_id))
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

                page_idx, total_pages = card_page_info(get_card_state(user_id, active))
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


def render_settings_text(user_id: int) -> str:
    """Body text shown on the top-level Settings screen."""
    s = session_manager.get_user_settings(user_id)
    return t(
        user_id,
        "settings.body",
        language=s.get("language", "en"),
        previews=s.get("previews", "economical"),
        live_lag=int(s.get("live_lag", 4)),
        voice=s.get("voice", "auto"),
    )


_GROUP_TEXT_KEYS: dict[str, str] = {
    "settings_previews": "settings.previews.body",
    "settings_lag": "settings.lag.body",
    "settings_voice": "settings.voice.body",
    "settings_language": "settings.lang.body",
    "settings_weeklyday": "settings.weeklyday.body",
    "settings_approve": "settings.approve.body",
    "settings_local": "settings.local.body",
    "settings_cardpos": "settings.cardpos.body",
}


def render_settings_group_text(user_id: int, screen: Screen) -> str:
    """Body text for a settings group sub-screen."""
    key = _GROUP_TEXT_KEYS.get(screen, "settings.title")
    return t(user_id, key)


def render_more_text(user_id: int) -> str:
    """Body text shown above the menu grid."""
    sess = session_manager.get_active_session(user_id)
    if sess is None:
        return t(user_id, "menu.empty")
    return t(user_id, "menu.active", name=sess.name or sess.id)
