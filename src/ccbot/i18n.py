"""Lightweight i18n: per-user UI strings in English / Russian / Chinese.

The active language is stored in `user_settings[user_id]["language"]` and
toggled via the inline ⚙ Settings → Language sub-screen. Anything not in
this surface (forwarded slash output, log messages, error details from the
shell) stays English regardless of the user's pick.

Public API:
  t(user_id, key, **fmt) -> str

The translation table is intentionally flat, dotted keys keep grouping
readable. Missing keys fall back to English; unknown languages fall back
to English as well.
"""

from __future__ import annotations

from typing import Any

from .session import session_manager

LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("ru", "Русский"),
    ("zh", "中文"),
)

# English source of truth — every key MUST be present here.
_EN: dict[str, str] = {
    # Footer buttons
    "btn.stop": "⏹ Stop",
    "btn.kill": "💀 Kill",
    "btn.clear": "🧹 Clear",
    "btn.menu": "≡ Menu",
    "btn.term": "🖥 Term",
    "btn.back": "← Back",
    "btn.cancel": "× Cancel",
    "btn.confirm": "✓ Confirm",
    "btn.no": "× No",
    "btn.yes_kill": "⚠ Yes, kill",
    "btn.yes_delete": "⚠ Yes, delete",
    "btn.refresh": "🔄 Refresh",
    "btn.save": "Saved",
    "btn.cancelled": "Cancelled",
    # Archive buttons
    "btn.restore": "⤴ Restore",
    "btn.restore_with_name": "⤴ Restore {name}",
    "btn.inspect": "🔍 Inspect",
    "btn.open_session": "📜 {name}",
    "btn.delete": "🗑 Delete",
    "btn.to_14d": "→ 14d",
    "btn.to_72h": "→ 72h",
    # More menu
    "mm.sessions": "📋 Sessions",
    "mm.status": "📊 Status",
    "mm.history": "📜 History",
    "mm.shot": "🧑‍💻 Shot",
    "mm.new": "🆕 New",
    "mm.archive": "🗄 Archive",
    "mm.settings": "⚙ Settings",
    # Menu screen body
    "menu.title": "*Menu*",
    "menu.empty": "*Menu*\n\nNo active session — pick one from the switcher or tap 🆕 New.",
    "menu.active": "*Menu* · active: *{name}*",
    # Settings — top
    "settings.title": "*Settings*",
    "settings.body": (
        "*Settings*\n\n"
        "Language: `{language}`\n"
        "Previews: `{previews}`\n"
        "Live lag: `{live_lag}s`\n"
        "Voice: `{voice}`\n\n"
        "_Tap a group to change._"
    ),
    # Settings — group labels (in the main grid)
    "settings.group.language": "Language",
    "settings.group.previews": "Previews",
    "settings.group.live_lag": "Live lag",
    "settings.group.voice": "Voice",
    # Settings — group sub-screen descriptions
    "settings.previews.body": (
        "*Previews*\n\n"
        "How session names are rendered in the picker:\n"
        "• `economical` — local fallback, no extra Claude calls\n"
        "• `readable` — Haiku-cached short summaries"
    ),
    "settings.lag.body": (
        "*Live preview lag*\n\n"
        "Coalescing window for live-card edits.\n"
        "`0s` = update on every event, higher = quieter chat."
    ),
    "settings.voice.body": (
        "*Voice transcription*\n\n"
        "Backend used for voice messages.\n"
        "• `auto` — Apple on macOS, whisper.cpp elsewhere\n"
        "• `whisper` — force whisper.cpp\n"
        "• `apple` — force Apple Speech (macOS only)\n"
        "• `off` — drop voice messages"
    ),
    "settings.lang.body": "*Language*\n\nUI language. Switches everything\nbut Claude's own output.",
    # Sessions list — only ``list.empty`` is still used (Menu → Sessions
    # empty-state when there's no active session). ``list.active`` /
    # ``list.lost`` are legacy.
    "list.empty": "No live sessions. Use 🆕 New to create one.",
    # Confirm dialogs
    "conf.kill": (
        "Kill *{name}*?\nTmux window dies, claude session id stored.\n"
        "Restore via the archive list."
    ),
    "conf.done": "Mark *{name}* as done?\nGoal closed, session archived.",
    "conf.delete": (
        "Delete *{name}* from archive?\nState record gone. JSONL kept on disk."
    ),
    "conf.killed": "💀 Killed `{name}`",
    "conf.done_ok": "🎉 Marked `{name}` as done.",
    "conf.deleted": "🗑 Archive entry deleted.",
    # Directory browser
    "dir.title": "*Select Working Directory*",
    "dir.current": "Current: `{path}`",
    "dir.empty": "_(No subdirectories)_",
    "dir.hint": "Tap a folder to enter, or select current directory",
    "dir.btn.up": "..",
    "dir.btn.select": "Select",
    # Session picker
    "picker.title": "*Resume Session?*",
    "picker.summary": "page {page}/{pages} — {total} session(s) in this directory.",
    "picker.btn.start_fresh": "🆕 Start fresh",
    "picker.btn.back_to_dirs": "← Back to dirs",
    # Inline toasts
    "toast.no_session": "No active session",
    "toast.window_gone": "Window gone",
    "toast.esc_sent": "⎋ Esc sent",
    "toast.cleared": "🧹 Context cleared",
    "toast.killed": "Killed",
    "toast.done": "Done",
    "toast.deleted": "Deleted",
    "toast.saved": "Saved",
    "toast.restored": "Restored",
    "toast.already_gone": "Already gone",
    "toast.nothing_to_kill": "Nothing to kill",
    "toast.term_opened": "🖥 Terminal opened",
    "toast.invalid_page": "Invalid page",
    "toast.session_not_found": "Session not found",
    "toast.restore_failed": "Restore failed: {msg}",
    "toast.range_14d": "→ 14d",
    "toast.range_72h": "→ 72h",
    # Archive screen
    "archive.title": "Archived sessions",
    "archive.range_72h": " (0–72h)",
    "archive.range_14d": " (0–14d)",
    "archive.empty": "No archived sessions in this window.",
    "archive.page_line": "page {page}/{pages} — {total} total",
    "archive.tokens_k": "{k}k tok",
    "archive.tokens_zero": "0 tok",
    "archive.age.s": "{n}s ago",
    "archive.age.m": "{n}m ago",
    "archive.age.h": "{n}h ago",
    "archive.age.d": "{n}d ago",
    # /usage compact display
    "usage.title": "*Claude Code*",
    "usage.unavailable": "Live usage unavailable.",
    "usage.5h": "5h",
    "usage.week": "week",
    "usage.week_sonnet": "week (Sonnet)",
    "usage.extra": "Extra",
    "usage.on": "on",
    "usage.off": "off",
    "usage.fetching": "Fetching usage…",
    # Settings group: weekly reset day
    "settings.group.weekly_reset_day": "Weekly reset",
    "settings.weeklyday.body": (
        "*Weekly reset day*\n\n"
        "Day of week the Anthropic weekly window resets.\n"
        "Used to compute the %/day burn rate on the weekly rows."
    ),
    "day.mon": "Mon",
    "day.tue": "Tue",
    "day.wed": "Wed",
    "day.thu": "Thu",
    "day.fri": "Fri",
    "day.sat": "Sat",
    "day.sun": "Sun",
    # Settings group: auto-approve interactive prompts
    "settings.group.auto_approve": "Auto-approve",
    "settings.approve.body": (
        "*Auto-approve*\n\n"
        "Bot's response to Claude Code's interactive Yes/No prompts\n"
        "that --dangerously-skip-permissions doesn't already bypass\n"
        "(e.g. WebFetch per-domain trust):\n"
        "• `off` — surface in chat, you tap manually\n"
        "• `on` — auto-Yes on every prompt"
    ),
    "approve.off": "off",
    "approve.on": "on",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "off",
    "local.manual": "manual",
    "local.auto": "auto",
    # Settings group: user-message disposition relative to the live card.
    "settings.group.card_position": "Card position",
    "settings.cardpos.body": (
        "*Card position*\n\n"
        "Where the live card sits after you type:\n"
        "• `push` — leave it (your message pushes the card up)\n"
        "• `delete` — delete your message so the card stays last\n"
        "• `repost` — resend the card below your message"
    ),
    "cardpos.push": "push",
    "cardpos.delete": "delete",
    "cardpos.repost": "repost",
    # Settings group: pop a native Terminal/iTerm window per new session
    "settings.group.local_terminal": "Local terminal",
    "settings.local.body": (
        "*Local terminal*\n\n"
        "Optional native desktop terminal attached to a session's "
        "tmux window — useful for driving Claude by hand in parallel "
        "with the Telegram UI.\n\n"
        "*off* — never spawn, never offer.\n"
        "*manual* — no auto-spawn; *🖥 Term* shows up next to *Stop / "
        "Kill / Clear / Menu* whenever the active session has no "
        "terminal attached.\n"
        "*auto* — spawn one on every new session AND show the same "
        "*🖥 Term* button whenever no terminal is attached.\n\n"
        "macOS: Terminal.app or iTerm2 (auto-detected).\n"
        "Linux: pick an emulator below. Tap *Configure via Claude* if "
        "the auto-detected list is wrong for your setup."
    ),
    "settings.local.claude_help": "🪄 Configure via Claude",
    # /help inline mini-doc
    "help.home.body": (
        "*Help*\n\n"
        "ccbot bridges this DM to N parallel Claude Code sessions running "
        "in tmux. Tap a section below for a quick tour."
    ),
    "help.btn.overview": "Overview",
    "help.btn.sessions": "Sessions",
    "help.btn.menu": "Menu",
    "help.btn.commands": "Commands",
    "help.btn.voice": "Voice & files",
    "help.btn.alerts": "Alerts",
    "help.btn.terminal": "Local terminal",
    "help.btn.tips": "Tips",
    "help.body.overview": (
        "*Overview*\n\n"
        "One private DM, many parallel Claude Code sessions. Send any "
        "text — it goes to your *active* session. Each session lives in "
        "its own tmux window with its own claude process; switching the "
        "active session never pauses the others.\n\n"
        "The inline keyboard under the most recent bot message hosts "
        "the session switcher and the ≡ Menu surface."
    ),
    "help.body.sessions": (
        "*Sessions*\n\n"
        "• *Create.* Send any text from an empty DM, or tap ≡ Menu → 🆕 "
        "New, then pick a project directory.\n"
        "• *Switch.* Tap a session button in the inline switcher under "
        "the latest bot message.\n"
        "• *Reply-quote.* Reply to a non-active session's bot message — "
        "your text is routed there for that one message only.\n"
        "• *Done.* `/done [name]` archives a session as completed.\n"
        "• *Idle TTL.* Sessions auto-archive after 4h with no input.\n"
        "• *Restore.* ≡ Menu → 📦 Archive → tap *Restore*."
    ),
    "help.body.menu": (
        "*≡ Menu*\n\n"
        "Open via /menu or the ≡ Menu inline button. Items:\n"
        "• 📋 *Sessions* — jump to the active session's live card\n"
        "• 📊 *Status* — Claude Code 5h / weekly / sonnet quotas\n"
        "• 🧑‍💻 *Shot* — terminal snapshot of the active session\n"
        "• 🆕 *New* — create a session from a directory browser\n"
        "• 📦 *Archive* — restore / inspect / delete archived sessions\n"
        "• ⚙ *Settings* — language, voice, local terminal, …"
    ),
    "help.body.commands": (
        "*Slash commands*\n\n"
        "Bot-side:\n"
        "• `/menu` — open the inline menu\n"
        "• `/help` — this help\n"
        "• `/done [name]` — archive a session\n"
        "• `/health` — uptime, queues, latency, counters\n\n"
        "Claude Code passthrough — any other `/cmd` is forwarded:\n"
        "• `/model` `/effort` `/clear` `/compact` `/cost` `/memory` …\n\n"
        "Type a leading `!` to capture local shell output and forward."
    ),
    "help.body.voice": (
        "*Voice & files*\n\n"
        "• *Voice.* Send a voice message — transcribed locally "
        "(whisper.cpp / Apple Speech) and routed to the active session "
        "as if you typed it.\n"
        "• *Photo / document.* Lands in `<workdir>/.ccbot-inbox/` and "
        "Claude is told via tmux. Files auto-clean after 24h; the "
        "Telegram `file_id` is retained for 30d for `/restore-file`."
    ),
    "help.body.alerts": (
        "*Alerts*\n\n"
        "*Quota alerts.* 5h / weekly / weekly-Sonnet quotas are sampled "
        "from the live `/usage` modal every 5 min. Bot pushes when % "
        "crosses 50, 75, or 90."
    ),
    "help.body.terminal": (
        "*Local terminal*\n\n"
        "Settings → Local terminal: when *on*, every new session pops "
        "a native window already attached to its tmux window — drive "
        "the session by hand from the desktop in parallel.\n\n"
        "macOS: Terminal.app / iTerm2 (auto, prefers iTerm tabs).\n"
        "Linux: pick an emulator from the auto-detected list, or use "
        "*Configure via Claude* for unusual setups.\n\n"
        "Direct attach also works any time: `tmux attach -t ccbot`."
    ),
    "help.body.tips": (
        "*Tips*\n\n"
        "• *Auto-approve.* Settings → Auto-approve auto-Yes's "
        "interactive prompts that --dangerously-skip-permissions "
        "doesn't already bypass (e.g. WebFetch domain trust).\n"
        "• *Card edit lag.* Settings → Live lag controls how often the "
        "live session card is re-edited (lower = snappier, higher = "
        "less rate-limit pressure).\n"
        "• *Languages.* Settings → Language: en / ru / zh.\n"
        "• *Outbound proxy.* Set `TG_PROXY_URL` if the host can't reach "
        "api.telegram.org directly."
    ),
}

_RU: dict[str, str] = {
    "btn.stop": "⏹ Стоп",
    "btn.kill": "💀 Убить",
    "btn.clear": "🧹 Очистить",
    "btn.menu": "≡ Меню",
    "btn.term": "🖥 Терминал",
    "btn.back": "← Назад",
    "btn.cancel": "× Отмена",
    "btn.confirm": "✓ Подтвердить",
    "btn.no": "× Нет",
    "btn.yes_kill": "⚠ Да, убить",
    "btn.yes_delete": "⚠ Да, удалить",
    "btn.refresh": "🔄 Обновить",
    "btn.save": "Сохранено",
    "btn.cancelled": "Отменено",
    # Archive buttons
    "btn.restore": "⤴ Восстановить",
    "btn.restore_with_name": "⤴ Восстановить {name}",
    "btn.inspect": "🔍 Просмотр",
    "btn.open_session": "📜 {name}",
    "btn.delete": "🗑 Удалить",
    "btn.to_14d": "→ 14д",
    "btn.to_72h": "→ 72ч",
    "mm.sessions": "📋 Сессии",
    "mm.status": "📊 Статус",
    "mm.history": "📜 История",
    "mm.shot": "🧑‍💻 Скрин",
    "mm.new": "🆕 Новая",
    "mm.archive": "🗄 Архив",
    "mm.settings": "⚙ Настройки",
    "menu.title": "*Меню*",
    "menu.empty": "*Меню*\n\nАктивной сессии нет — выбери в свитчере или тапни 🆕 Новая.",
    "menu.active": "*Меню* · активна: *{name}*",
    "settings.title": "*Настройки*",
    "settings.body": (
        "*Настройки*\n\n"
        "Язык: `{language}`\n"
        "Превью: `{previews}`\n"
        "Лаг карточки: `{live_lag}с`\n"
        "Голос: `{voice}`\n\n"
        "_Тапни группу, чтобы изменить._"
    ),
    "settings.group.language": "Язык",
    "settings.group.previews": "Превью",
    "settings.group.live_lag": "Лаг карточки",
    "settings.group.voice": "Голос",
    "settings.previews.body": (
        "*Превью*\n\n"
        "Как именуются сессии в пикере:\n"
        "• `economical` — локальный fallback, без обращений к Claude\n"
        "• `readable` — короткие саммари через Haiku, кэшируется"
    ),
    "settings.lag.body": (
        "*Лаг карточки*\n\n"
        "Окно сглаживания правок live-карточки.\n"
        "`0с` = править на каждом событии, больше = тише в чате."
    ),
    "settings.voice.body": (
        "*Распознавание голоса*\n\n"
        "Бэкенд для voice-сообщений.\n"
        "• `auto` — Apple на macOS, whisper.cpp иначе\n"
        "• `whisper` — форсить whisper.cpp\n"
        "• `apple` — форсить Apple Speech (только macOS)\n"
        "• `off` — игнорировать voice"
    ),
    "settings.lang.body": (
        "*Язык*\n\nЯзык интерфейса. Переключает всё,\nкроме самого вывода Claude."
    ),
    "list.empty": "Активных сессий нет. Тапни 🆕 Новая, чтобы создать.",
    "conf.kill": (
        "Убить *{name}*?\nTmux-окно умрёт, claude session id сохранится.\n"
        "Восстановить можно через архив."
    ),
    "conf.done": "Закрыть *{name}*?\nЦель закрыта, сессия в архиве.",
    "conf.delete": (
        "Удалить *{name}* из архива?\nЗапись стирается. JSONL остаётся на диске."
    ),
    "conf.killed": "💀 Убита `{name}`",
    "conf.done_ok": "🎉 `{name}` закрыта.",
    "conf.deleted": "🗑 Запись из архива удалена.",
    "dir.title": "*Выбор рабочей директории*",
    "dir.current": "Текущая: `{path}`",
    "dir.empty": "_(Поддиректорий нет)_",
    "dir.hint": "Тапни папку, чтобы войти, или выбери текущую",
    "dir.btn.up": "..",
    "dir.btn.select": "Выбрать",
    "picker.title": "*Возобновить сессию?*",
    "picker.summary": "стр. {page}/{pages} — {total} сессий в этой папке.",
    "picker.btn.start_fresh": "🆕 С нуля",
    "picker.btn.back_to_dirs": "← К папкам",
    "toast.no_session": "Нет активной сессии",
    "toast.window_gone": "Окно исчезло",
    "toast.esc_sent": "⎋ Esc отправлен",
    "toast.cleared": "🧹 Контекст очищен",
    "toast.killed": "Убита",
    "toast.done": "Закрыта",
    "toast.deleted": "Удалена",
    "toast.saved": "Сохранено",
    "toast.restored": "Восстановлена",
    "toast.already_gone": "Уже нет",
    "toast.nothing_to_kill": "Убивать нечего",
    "toast.term_opened": "🖥 Терминал открыт",
    "toast.invalid_page": "Неверная страница",
    "toast.session_not_found": "Сессия не найдена",
    "toast.restore_failed": "Не удалось восстановить: {msg}",
    "toast.range_14d": "→ 14д",
    "toast.range_72h": "→ 72ч",
    # Archive screen
    "archive.title": "Архивные сессии",
    "archive.range_72h": " (0–72ч)",
    "archive.range_14d": " (0–14д)",
    "archive.empty": "Архивных сессий в этом окне нет.",
    "archive.page_line": "стр. {page}/{pages} — всего {total}",
    "archive.tokens_k": "{k}k токенов",
    "archive.tokens_zero": "0 токенов",
    "archive.age.s": "{n}с назад",
    "archive.age.m": "{n}мин назад",
    "archive.age.h": "{n}ч назад",
    "archive.age.d": "{n}д назад",
    "usage.title": "*Claude Code*",
    "usage.unavailable": "Живые данные usage недоступны.",
    "usage.5h": "5ч",
    "usage.week": "неделя",
    "usage.week_sonnet": "неделя (Sonnet)",
    "usage.extra": "Extra",
    "usage.on": "вкл",
    "usage.off": "выкл",
    "usage.fetching": "Тяну usage…",
    "settings.group.weekly_reset_day": "Сброс недели",
    "settings.weeklyday.body": (
        "*День сброса недели*\n\n"
        "День недели, в который сбрасывается недельная квота Anthropic.\n"
        "Используется для расчёта %/день в weekly-строках."
    ),
    "day.mon": "пн",
    "day.tue": "вт",
    "day.wed": "ср",
    "day.thu": "чт",
    "day.fri": "пт",
    "day.sat": "сб",
    "day.sun": "вс",
    "settings.group.auto_approve": "Авто-подтверждение",
    "settings.approve.body": (
        "*Авто-подтверждение*\n\n"
        "Как боту обращаться с интерактивными Yes/No-промптами,\n"
        "которые --dangerously-skip-permissions сам не закрывает\n"
        "(например, доверие домену для WebFetch):\n"
        "• `off` — присылать в чат, ты тапаешь сам\n"
        "• `on` — Yes на любой промпт"
    ),
    "approve.off": "выкл",
    "approve.on": "вкл",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "выкл",
    "local.manual": "по кнопке",
    "local.auto": "всегда",
    "settings.group.card_position": "Положение карточки",
    "settings.cardpos.body": (
        "*Положение карточки*\n\n"
        "Где остаётся живая карточка после твоего сообщения:\n"
        "• `push` — оставить как есть (твоё сообщение сдвигает карточку вверх)\n"
        "• `delete` — удалить твоё сообщение, чтобы карточка оставалась последней\n"
        "• `repost` — переслать карточку под твоё сообщение"
    ),
    "cardpos.push": "push",
    "cardpos.delete": "удалить",
    "cardpos.repost": "переслать",
    "settings.group.local_terminal": "Локальный терминал",
    "settings.local.body": (
        "*Локальный терминал*\n\n"
        "Опциональное нативное окно с `tmux attach` к сессии —\n"
        "удобно вести Claude руками с десктопа параллельно\n"
        "с Telegram.\n\n"
        "*выкл* — никогда не открывать, кнопку не показывать.\n"
        "*по кнопке* — авто-спавна нет; *🖥 Терминал*\n"
        "появляется рядом со *Стоп / Убить / Очистить / Меню*\n"
        "когда у активной сессии терминал не аттачен.\n"
        "*всегда* — спавнить при создании каждой сессии И\n"
        "показывать ту же *🖥 Терминал*-кнопку, когда\n"
        "терминала нет.\n\n"
        "macOS: Terminal.app или iTerm2 (авто).\n"
        "Linux: выбери эмулятор ниже. Тапни *Configure via Claude*\n"
        "если автодетект не угадал."
    ),
    "settings.local.claude_help": "🪄 Настроить через Claude",
    "help.home.body": (
        "*Помощь*\n\n"
        "ccbot связывает этот личный чат с N параллельными сессиями "
        "Claude Code в tmux. Тапни нужный раздел ниже."
    ),
    "help.btn.overview": "Обзор",
    "help.btn.sessions": "Сессии",
    "help.btn.menu": "Меню",
    "help.btn.commands": "Команды",
    "help.btn.voice": "Голос и файлы",
    "help.btn.alerts": "Алерты",
    "help.btn.terminal": "Локальный терминал",
    "help.btn.tips": "Советы",
    "help.body.overview": (
        "*Обзор*\n\n"
        "Один личный DM, много параллельных сессий Claude Code. Любой "
        "текст летит в *активную* сессию. У каждой сессии своё tmux-окно "
        "и свой процесс claude — переключение активной не ставит другие "
        "на паузу.\n\n"
        "Инлайн-клавиатура под последним сообщением бота — это "
        "переключатель сессий и ≡ Меню."
    ),
    "help.body.sessions": (
        "*Сессии*\n\n"
        "• *Создать.* Просто отправь любой текст в пустой DM, или "
        "≡ Меню → 🆕 New, выбери директорию.\n"
        "• *Переключить.* Тапни кнопку сессии в инлайн-переключателе.\n"
        "• *Reply-quote.* Ответь (Telegram-цитата) на сообщение бота из "
        "неактивной сессии — твой текст уйдёт туда разово, без смены "
        "активной.\n"
        "• *Закрыть.* `/done [имя]` — отмечает сессию как готовую.\n"
        "• *Idle TTL.* Без ввода 4 часа — авто-архив.\n"
        "• *Восстановить.* ≡ Меню → 📦 Archive → *Restore*."
    ),
    "help.body.menu": (
        "*≡ Меню*\n\n"
        "Открывается через /menu или инлайн-кнопку ≡. Пункты:\n"
        "• 📋 *Sessions* — переход на живую карточку активной\n"
        "• 📊 *Status* — лимиты Claude Code (5ч / неделя / sonnet)\n"
        "• 🧑‍💻 *Shot* — снимок терминала активной сессии\n"
        "• 🆕 *New* — создать сессию через выбор директории\n"
        "• 📦 *Archive* — восстановить / посмотреть / удалить\n"
        "• ⚙ *Settings* — язык, голос, терминал, ..."
    ),
    "help.body.commands": (
        "*Слэш-команды*\n\n"
        "Бот:\n"
        "• `/menu` — открыть инлайн-меню\n"
        "• `/help` — эта справка\n"
        "• `/done [имя]` — архивировать сессию\n"
        "• `/health` — uptime, очереди, latency, счётчики\n\n"
        "Claude Code (форвардятся как есть):\n"
        "• `/model` `/effort` `/clear` `/compact` `/cost` `/memory` …\n\n"
        "Префикс `!` — захват вывода локальной шелл-команды и форвард."
    ),
    "help.body.voice": (
        "*Голос и файлы*\n\n"
        "• *Голос.* Отправь голосовое — оно расшифровывается локально "
        "(whisper.cpp / Apple Speech) и уходит в активную сессию как "
        "текст.\n"
        "• *Фото / документ.* Кладётся в `<workdir>/.ccbot-inbox/`, "
        "Claude получает синтетическое сообщение через tmux. TTL 24ч; "
        "Telegram `file_id` хранится 30д для `/restore-file`."
    ),
    "help.body.alerts": (
        "*Алерты*\n\n"
        "*Квоты Claude Code.* 5ч / неделя / неделя Sonnet — бот опрашивает "
        "живой `/usage` каждые 5 мин и пушит при пересечении 50, 75, 90 %."
    ),
    "help.body.terminal": (
        "*Локальный терминал*\n\n"
        "Settings → Local terminal: при *on* каждая новая сессия "
        "автоматически открывает нативное окно, уже привязанное к её "
        "tmux-window — управляй с десктопа параллельно с Telegram.\n\n"
        "macOS: Terminal.app / iTerm2 (auto, предпочитает вкладки в iTerm).\n"
        "Linux: выбор эмулятора из списка, либо *Configure via Claude* "
        "для нестандартных кейсов.\n\n"
        "В любой момент работает прямой `tmux attach -t ccbot`."
    ),
    "help.body.tips": (
        "*Советы*\n\n"
        "• *Auto-approve.* Settings → Auto-approve авто-Yes-ит модалки, "
        "которые --dangerously-skip-permissions не закрывает сам "
        "(WebFetch domain trust и т.п.).\n"
        "• *Live lag.* Settings → Live lag — частота перерисовки "
        "карточки сессии. Меньше = шустрее, больше = меньше rate-limit.\n"
        "• *Языки.* Settings → Language: en / ru / zh.\n"
        "• *Outbound proxy.* `TG_PROXY_URL` если api.telegram.org "
        "недоступен напрямую."
    ),
}

_ZH: dict[str, str] = {
    "btn.stop": "⏹ 停止",
    "btn.kill": "💀 终止",
    "btn.clear": "🧹 清空",
    "btn.menu": "≡ 菜单",
    "btn.term": "🖥 终端",
    "btn.back": "← 返回",
    "btn.cancel": "× 取消",
    "btn.confirm": "✓ 确认",
    "btn.no": "× 否",
    "btn.yes_kill": "⚠ 是，终止",
    "btn.yes_delete": "⚠ 是，删除",
    "btn.refresh": "🔄 刷新",
    "btn.save": "已保存",
    "btn.cancelled": "已取消",
    # Archive buttons
    "btn.restore": "⤴ 恢复",
    "btn.restore_with_name": "⤴ 恢复 {name}",
    "btn.inspect": "🔍 查看",
    "btn.open_session": "📜 {name}",
    "btn.delete": "🗑 删除",
    "btn.to_14d": "→ 14天",
    "btn.to_72h": "→ 72时",
    "mm.sessions": "📋 会话",
    "mm.status": "📊 状态",
    "mm.history": "📜 历史",
    "mm.shot": "🧑‍💻 截图",
    "mm.new": "🆕 新建",
    "mm.archive": "🗄 归档",
    "mm.settings": "⚙ 设置",
    "menu.title": "*菜单*",
    "menu.empty": "*菜单*\n\n无活动会话——从切换器选一个或点 🆕 新建。",
    "menu.active": "*菜单* · 活动: *{name}*",
    "settings.title": "*设置*",
    "settings.body": (
        "*设置*\n\n"
        "语言: `{language}`\n"
        "预览: `{previews}`\n"
        "卡片延迟: `{live_lag}秒`\n"
        "语音: `{voice}`\n\n"
        "_点击分组进行更改。_"
    ),
    "settings.group.language": "语言",
    "settings.group.previews": "预览",
    "settings.group.live_lag": "卡片延迟",
    "settings.group.voice": "语音",
    "settings.previews.body": (
        "*预览*\n\n"
        "选择器中如何呈现会话名:\n"
        "• `economical` — 本地回退,不额外调用 Claude\n"
        "• `readable` — Haiku 缓存的简短摘要"
    ),
    "settings.lag.body": (
        "*实时预览延迟*\n\n"
        "实时卡片编辑的合并窗口。\n"
        "`0秒` = 每个事件都更新,数值越高越安静。"
    ),
    "settings.voice.body": (
        "*语音识别*\n\n"
        "语音消息使用的后端。\n"
        "• `auto` — macOS 用 Apple, 其他用 whisper.cpp\n"
        "• `whisper` — 强制 whisper.cpp\n"
        "• `apple` — 强制 Apple Speech (仅 macOS)\n"
        "• `off` — 忽略语音"
    ),
    "settings.lang.body": "*语言*\n\n界面语言。切换除 Claude 自身输出外的一切文本。",
    "list.empty": "没有活动会话。点 🆕 新建以创建。",
    "conf.kill": (
        "终止 *{name}*?\nTmux 窗口结束,claude session id 已保存。\n可通过归档列表恢复。"
    ),
    "conf.done": "标记 *{name}* 为完成?\n目标已关闭,会话已归档。",
    "conf.delete": "从归档中删除 *{name}*?\n状态记录消失。JSONL 保留在磁盘。",
    "conf.killed": "💀 已终止 `{name}`",
    "conf.done_ok": "🎉 `{name}` 已标记完成。",
    "conf.deleted": "🗑 归档记录已删除。",
    "dir.title": "*选择工作目录*",
    "dir.current": "当前: `{path}`",
    "dir.empty": "_(无子目录)_",
    "dir.hint": "点文件夹进入,或选择当前目录",
    "dir.btn.up": "..",
    "dir.btn.select": "选择",
    "picker.title": "*恢复会话?*",
    "picker.summary": "第 {page}/{pages} 页 — 此目录共 {total} 个会话。",
    "picker.btn.start_fresh": "🆕 从零开始",
    "picker.btn.back_to_dirs": "← 返回目录",
    "toast.no_session": "无活动会话",
    "toast.window_gone": "窗口已消失",
    "toast.esc_sent": "⎋ 已发送 Esc",
    "toast.cleared": "🧹 上下文已清空",
    "toast.killed": "已终止",
    "toast.done": "已完成",
    "toast.deleted": "已删除",
    "toast.saved": "已保存",
    "toast.restored": "已恢复",
    "toast.already_gone": "已不存在",
    "toast.nothing_to_kill": "没什么可终止的",
    "toast.term_opened": "🖥 已打开终端",
    "toast.invalid_page": "页面无效",
    "toast.session_not_found": "未找到会话",
    "toast.restore_failed": "恢复失败:{msg}",
    "toast.range_14d": "→ 14天",
    "toast.range_72h": "→ 72时",
    # Archive screen
    "archive.title": "已归档会话",
    "archive.range_72h": "(0–72时)",
    "archive.range_14d": "(0–14天)",
    "archive.empty": "此范围内没有已归档会话。",
    "archive.page_line": "第 {page}/{pages} 页 — 共 {total}",
    "archive.tokens_k": "{k}k 词元",
    "archive.tokens_zero": "0 词元",
    "archive.age.s": "{n}秒前",
    "archive.age.m": "{n}分前",
    "archive.age.h": "{n}时前",
    "archive.age.d": "{n}天前",
    "usage.title": "*Claude Code*",
    "usage.unavailable": "实时使用数据不可用。",
    "usage.5h": "5小时",
    "usage.week": "本周",
    "usage.week_sonnet": "本周 (Sonnet)",
    "usage.extra": "Extra",
    "usage.on": "开",
    "usage.off": "关",
    "usage.fetching": "正在获取使用情况…",
    "settings.group.weekly_reset_day": "周重置",
    "settings.weeklyday.body": (
        "*每周重置日*\n\n"
        "Anthropic 周配额重置的星期。\n"
        "用于计算 weekly 行的 %/天 消耗速率。"
    ),
    "day.mon": "一",
    "day.tue": "二",
    "day.wed": "三",
    "day.thu": "四",
    "day.fri": "五",
    "day.sat": "六",
    "day.sun": "日",
    "settings.group.auto_approve": "自动同意",
    "settings.approve.body": (
        "*自动同意*\n\n"
        "对 --dangerously-skip-permissions 未覆盖的\n"
        "Claude Code 交互式 Yes/No 提示的处理方式\n"
        "(例如 WebFetch 域名信任):\n"
        "• `off` — 推送到聊天,手动点击\n"
        "• `on` — 所有提示自动 Yes"
    ),
    "approve.off": "关",
    "approve.on": "开",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "关",
    "local.manual": "按钮",
    "local.auto": "总是",
    "settings.group.card_position": "卡片位置",
    "settings.cardpos.body": (
        "*卡片位置*\n\n"
        "你输入后,实时卡片的位置:\n"
        "• `push` — 保持原样(你的消息把卡片推上去)\n"
        "• `delete` — 删除你的消息,卡片留在最下方\n"
        "• `repost` — 在你消息下方重新发送卡片"
    ),
    "cardpos.push": "push",
    "cardpos.delete": "删除",
    "cardpos.repost": "重发",
    "settings.group.local_terminal": "本地终端",
    "settings.local.body": (
        "*本地终端*\n\n"
        "可选的本地终端,附加到会话的 tmux 窗口 ——\n"
        "便于在桌面手动操作 Claude,与 Telegram 并行。\n\n"
        "*关* — 从不打开,不显示按钮。\n"
        "*按钮* — 不自动打开;当活动会话未附加终端时,\n"
        "*🖥 终端* 出现在 *停止 / 终止 / 清空 / 菜单* 旁边。\n"
        "*总是* — 每个新会话都自动打开,同时在未附加\n"
        "终端时显示相同的 *🖥 终端* 按钮。\n\n"
        "macOS:Terminal.app 或 iTerm2(自动)。\n"
        "Linux:在下方选择终端模拟器。如果自动检测\n"
        "不符合实际环境,请点击 *Configure via Claude*。"
    ),
    "settings.local.claude_help": "🪄 通过 Claude 配置",
    "help.home.body": (
        "*帮助*\n\n"
        "ccbot 将这个私聊连接到 N 个并行运行在 tmux 中的\n"
        "Claude Code 会话。点击下方对应章节查看简介。"
    ),
    "help.btn.overview": "概览",
    "help.btn.sessions": "会话",
    "help.btn.menu": "菜单",
    "help.btn.commands": "命令",
    "help.btn.voice": "语音和文件",
    "help.btn.alerts": "提醒",
    "help.btn.terminal": "本地终端",
    "help.btn.tips": "技巧",
    "help.body.overview": (
        "*概览*\n\n"
        "一个私聊,多个并行的 Claude Code 会话。任何文本会发送到\n"
        "当前的 *活动* 会话。每个会话拥有独立的 tmux 窗口和 claude\n"
        "进程,切换活动会话不会暂停其他会话。\n\n"
        "最新机器人消息下方的内联键盘是会话切换器和 ≡ 菜单。"
    ),
    "help.body.sessions": (
        "*会话*\n\n"
        "• *创建。* 在空 DM 中发送任意文本,或 ≡ 菜单 → 🆕 New,\n"
        "选择一个目录。\n"
        "• *切换。* 点击切换器中的会话按钮。\n"
        "• *引用回复。* 回复非活动会话的机器人消息 — 你的文本\n"
        "只单次路由到该会话,不更改活动状态。\n"
        "• *完成。* `/done [name]` — 标记并归档。\n"
        "• *闲置 TTL。* 4 小时无输入自动归档。\n"
        "• *恢复。* ≡ 菜单 → 📦 Archive → *Restore*。"
    ),
    "help.body.menu": (
        "*≡ 菜单*\n\n"
        "通过 /menu 或 ≡ 菜单内联按钮打开:\n"
        "• 📋 *Sessions* — 跳转到当前会话的实时卡片\n"
        "• 📊 *Status* — 5h / 周 / sonnet 配额\n"
        "• 🧑‍💻 *Shot* — 当前会话的终端快照\n"
        "• 🆕 *New* — 通过目录浏览器创建会话\n"
        "• 📦 *Archive* — 恢复 / 查看 / 删除\n"
        "• ⚙ *Settings* — 语言 / 语音 / 终端 …"
    ),
    "help.body.commands": (
        "*斜杠命令*\n\n"
        "Bot 端:\n"
        "• `/menu` — 打开内联菜单\n"
        "• `/help` — 本帮助\n"
        "• `/done [name]` — 归档会话\n"
        "• `/health` — 运行时间 / 队列 / 延迟 / 计数器\n\n"
        "Claude Code 透传(原样转发):\n"
        "• `/model` `/effort` `/clear` `/compact` `/cost` `/memory` …\n\n"
        "前缀 `!` — 捕获本地 shell 命令的输出并转发。"
    ),
    "help.body.voice": (
        "*语音和文件*\n\n"
        "• *语音。* 发送语音消息 — 在本地转写\n"
        "(whisper.cpp / Apple Speech)然后作为文本发送给活动会话。\n"
        "• *照片 / 文档。* 落到 `<workdir>/.ccbot-inbox/`,Claude 通过\n"
        "tmux 收到合成消息。TTL 24 小时;Telegram `file_id` 保留 30 天\n"
        "用于 `/restore-file`。"
    ),
    "help.body.alerts": (
        "*提醒*\n\n"
        "*配额提醒。* 5h / 周 / 周-Sonnet 配额 — 机器人每 5 分钟轮询\n"
        "实时 `/usage` 弹窗,百分比跨过 50 / 75 / 90 时推送。"
    ),
    "help.body.terminal": (
        "*本地终端*\n\n"
        "Settings → Local terminal:开启后,每次新建会话也会弹出\n"
        "本地原生窗口,自动 attach 到对应 tmux 窗口 —\n"
        "桌面手动操作和 Telegram 并行。\n\n"
        "macOS:Terminal.app / iTerm2(自动,iTerm 优先用 tab)。\n"
        "Linux:从自动检测列表选择,或 *Configure via Claude*\n"
        "处理特殊环境。\n\n"
        "随时也可直接 `tmux attach -t ccbot`。"
    ),
    "help.body.tips": (
        "*技巧*\n\n"
        "• *自动同意。* Settings → Auto-approve 自动 Yes\n"
        "--dangerously-skip-permissions 未覆盖的提示。\n"
        "• *Live lag。* Settings → Live lag — 会话卡片重绘频率,\n"
        "更小 = 更灵敏,更大 = 更省 rate-limit。\n"
        "• *语言。* Settings → Language:en / ru / zh。\n"
        "• *出站代理。* `TG_PROXY_URL` 如果主机无法\n"
        "直接访问 api.telegram.org。"
    ),
}

TRANSLATIONS: dict[str, dict[str, str]] = {
    "en": _EN,
    "ru": _RU,
    "zh": _ZH,
}


def get_user_lang(user_id: int) -> str:
    """Resolve the user's language code, falling back to 'en'."""
    settings = session_manager.get_user_settings(user_id)
    code = settings.get("language", "en")
    if code not in TRANSLATIONS:
        return "en"
    return code


def t(user_id: int, key: str, **fmt: Any) -> str:
    """Translate `key` for the user. Falls back to English on missing key.

    `fmt` kwargs are passed to str.format on the resolved template.
    """
    lang = get_user_lang(user_id)
    table = TRANSLATIONS.get(lang) or _EN
    template = table.get(key) or _EN.get(key) or key
    if fmt:
        try:
            return template.format(**fmt)
        except (KeyError, IndexError):
            return template
    return template
