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
    "btn.back": "← Back",
    "btn.cancel": "× Cancel",
    "btn.confirm": "✓ Confirm",
    "btn.no": "× No",
    "btn.yes_kill": "⚠ Yes, kill",
    "btn.yes_delete": "⚠ Yes, delete",
    "btn.refresh": "🔄 Refresh",
    "btn.save": "Saved",
    "btn.cancelled": "Cancelled",
    # More menu
    "mm.list": "📋 List",
    "mm.status": "📊 Status",
    "mm.history": "📜 History",
    "mm.shot": "📸 Shot",
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
    # /list
    "list.active": "*Active*",
    "list.lost": "*Lost*",
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
    # Settings group: per-session token alert thresholds
    "settings.group.token_alerts": "Token alerts",
    "settings.tokens.body": (
        "*Per-session token alerts*\n\n"
        "Three thresholds. The bot pushes one notification each time a "
        "session's lifetime token usage crosses one of these — adjust in "
        "50k-token steps."
    ),
    # Settings group: pop a native Terminal/iTerm window per new session
    "settings.group.local_terminal": "Local terminal",
    "settings.local.body": (
        "*Local terminal*\n\n"
        "When `on`, every new session also opens a native macOS "
        "Terminal/iTerm window attached to its tmux window — drive "
        "the session by hand from the desktop in parallel with the "
        "Telegram UI. No-op on non-macOS hosts."
    ),
}

_RU: dict[str, str] = {
    "btn.stop": "⏹ Стоп",
    "btn.kill": "💀 Убить",
    "btn.clear": "🧹 Очистить",
    "btn.menu": "≡ Меню",
    "btn.back": "← Назад",
    "btn.cancel": "× Отмена",
    "btn.confirm": "✓ Подтвердить",
    "btn.no": "× Нет",
    "btn.yes_kill": "⚠ Да, убить",
    "btn.yes_delete": "⚠ Да, удалить",
    "btn.refresh": "🔄 Обновить",
    "btn.save": "Сохранено",
    "btn.cancelled": "Отменено",
    "mm.list": "📋 Список",
    "mm.status": "📊 Статус",
    "mm.history": "📜 История",
    "mm.shot": "📸 Скрин",
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
    "list.active": "*Активные*",
    "list.lost": "*Потерянные*",
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
    "settings.group.token_alerts": "Алерты по токенам",
    "settings.tokens.body": (
        "*Алерты по токенам сессии*\n\n"
        "Три порога. Бот один раз пришлёт уведомление, когда\n"
        "суммарный расход сессии переходит каждый — шаг 50k."
    ),
    "settings.group.local_terminal": "Локальный терминал",
    "settings.local.body": (
        "*Локальный терминал*\n\n"
        "Если `on`, при создании каждой сессии бот открывает\n"
        "нативное окно Terminal/iTerm с `tmux attach` — управляй\n"
        "сессией с десктопа параллельно с Telegram. Только macOS."
    ),
}

_ZH: dict[str, str] = {
    "btn.stop": "⏹ 停止",
    "btn.kill": "💀 终止",
    "btn.clear": "🧹 清空",
    "btn.menu": "≡ 菜单",
    "btn.back": "← 返回",
    "btn.cancel": "× 取消",
    "btn.confirm": "✓ 确认",
    "btn.no": "× 否",
    "btn.yes_kill": "⚠ 是，终止",
    "btn.yes_delete": "⚠ 是，删除",
    "btn.refresh": "🔄 刷新",
    "btn.save": "已保存",
    "btn.cancelled": "已取消",
    "mm.list": "📋 列表",
    "mm.status": "📊 状态",
    "mm.history": "📜 历史",
    "mm.shot": "📸 截图",
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
    "list.active": "*活动*",
    "list.lost": "*丢失*",
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
    "settings.group.token_alerts": "Token 提醒",
    "settings.tokens.body": (
        "*会话 Token 提醒*\n\n"
        "三个阈值。每当会话累计 token 跨过一个阈值时,\n"
        "机器人发送一次推送通知,步长 50k。"
    ),
    "settings.group.local_terminal": "本地终端",
    "settings.local.body": (
        "*本地终端*\n\n"
        "开启后,每当创建新会话,机器人也会在 macOS\n"
        "Terminal/iTerm 中打开一个附加到 tmux 窗口的本地\n"
        "终端窗口,可与 Telegram 并行手动操作。仅限 macOS。"
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
