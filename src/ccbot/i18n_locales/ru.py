"""Russian translation table for the Telegram UI."""

from __future__ import annotations

RU: dict[str, str] = {
    "voice.not_delivered": (
        "🎙 Голос не попал в сессию — в ней открыт запрос (опрув/вопрос), "
        "и распознанный текст туда не уходит. Ответь на запрос и перешли "
        "голосовое ещё раз."
    ),
    "voice.download_failed": (
        "🎙 Голосовое не дошло до сессии: Telegram не отдал аудиофайл после "
        "{attempts} попыток. Отправь голосовое ещё раз."
    ),
    "voice.transcription_failed": (
        "🎙 Голосовое не удалось распознать, и оно не дошло до сессии. "
        "Отправь его ещё раз."
    ),
    "voice.transcribing": "🎙 Голосовое распознаётся…",
    "voice.queued_dropped": "Последующие сообщения тоже не дошли до сессии.",
    "btn.stop": "⏹ Стоп",
    "btn.kill": "💀 Убить",
    "btn.clear": "🧹 Очистить",
    "btn.menu": "≡ Меню",
    "btn.term": "🖥 Терминал",
    "btn.back": "← Назад",
    "btn.cancel": "× Отмена",
    "btn.login": "🔐 Войти",
    # Claude re-authentication (/login)
    "auth.expired": (
        "🔐 *Авторизация Claude слетела*\n\n"
        "Все сессии на этом хосте будут падать, пока логин не обновлён. Сам "
        "бот при этом жив — он и проведёт тебя через процедуру.\n\n"
        "Отправь /login (или нажми кнопку): я дам ссылку, ты подтверждаешь в "
        "браузере и присылаешь код сюда."
    ),
    "auth.login.starting": "🔐 Запускаю процедуру логина…",
    "auth.login.url": (
        "🔐 *Шаг 1/2* — открой и подтверди:\n\n"
        "{url}\n\n"
        "*Шаг 2/2* — на странице будет код. Пришли его сюда обычным "
        "сообщением. Ссылка живёт 15 минут."
    ),
    "auth.login.no_url": (
        "❌ Не удалось получить ссылку логина от CLI. Попробуй /login ещё раз; "
        "если повторяется — выполни `claude auth login` на хосте."
    ),
    "auth.login.ok": (
        "✅ *Готово.* Авторизация продлена до {deadline}.\n\n"
        "Падавшие сессии заработают со следующего сообщения."
    ),
    "auth.login.failed": "❌ Код не принят: {detail}\n\nОтправь /login, чтобы повторить.",
    "auth.login.cancelled": "Логин отменён.",
    "auth.codex.device": (
        "🔐 *Авторизация Codex*\n\n"
        "1. Открой {url}\n"
        "2. Введи код: `{code}`\n\n"
        "Бот сам увидит подтверждение; присылать код сюда не нужно. "
        "Код действует около 15 минут."
    ),
    "auth.codex.no_device_code": (
        "❌ Codex не выдал device code. Проверь, что установлен актуальный "
        "Codex CLI, и повтори /login."
    ),
    "auth.codex.ok": (
        "✅ *Codex авторизован.* Теперь можно создавать и возобновлять сессии."
    ),
    "auth.codex.failed": (
        "❌ Авторизация Codex не завершена: {detail}\n\nПовтори /login."
    ),
    "auth.codex.waiting": (
        "🔐 Codex всё ещё ждёт подтверждения в браузере. Используй ссылку и код выше."
    ),
    "auth.codex.required": (
        "🔐 Авторизуй Codex по ссылке выше, затем повтори создание сессии."
    ),
    "auth.codex.check_failed": (
        "❌ Не удалось проверить авторизацию Codex. Проверь `codex --version` "
        "и `CODEX_COMMAND`, затем отправь /login."
    ),
    "auth.codex.storage_mismatch": (
        "⚠ Codex нашел `auth.json`, но effective credential storage его не "
        'читает. Установи `cli_auth_credentials_store = "file"`; бот не '
        "будет заменять существующую авторизацию."
    ),
    "btn.confirm": "✓ Подтвердить",
    "btn.no": "× Нет",
    "btn.yes_kill": "⚠ Да, убить",
    "btn.yes_delete": "⚠ Да, удалить",
    "btn.yes_clear": "⚠ Да, очистить",
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
        "Агент: `{agent}`\n"
        "Язык: `{language}`\n"
        "Лаг карточки: `{live_lag}с`\n"
        "Голос: `{voice}`\n\n"
        "_Тапни группу, чтобы изменить._"
    ),
    "settings.group.agent": "Агент",
    "settings.group.language": "Язык",
    "settings.group.live_lag": "Лаг карточки",
    "settings.group.voice": "Голос",
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
    "settings.agent.body": (
        "*Агент*\n\n"
        "Глобальный backend для всего бота. Все новые сессии работают либо "
        "через *Claude*, либо через *Codex*.\n\n"
        "Переключение заблокировано, пока остаются живые сессии текущего "
        "агента. Сначала заверши или архивируй их."
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
    "conf.clear": (
        "Очистить *{name}*?\nОтправит Esc, затем /clear. Контекст сессии "
        "стирается без возможности восстановления (в отличие от Kill → Restore)."
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
    "toast.agent_live": (
        "Перед сменой глобального агента заверши или архивируй все живые сессии."
    ),
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
    "archive.age.s": "{n}с назад",
    "archive.age.m": "{n}мин назад",
    "archive.age.h": "{n}ч назад",
    "archive.age.d": "{n}д назад",
    "usage.title": "*Claude Code*",
    "usage.title.codex": "*OpenAI Codex*",
    "usage.unavailable": "Живые данные usage недоступны.",
    "usage.auth_required": (
        "Для загрузки Usage нужна авторизация Codex. Заверши вход по сообщению "
        "выше, затем обнови этот экран."
    ),
    "usage.5h": "5ч",
    "usage.week": "неделя",
    "usage.week_sonnet": "неделя (Sonnet)",
    "usage.not_reported": "Codex не передал",
    "usage.today": "Сегодня",
    "usage.today_left": "ещё",
    "usage.today_overspent": "перерасход",
    "usage.used": "Использовано",
    "usage.reset": "Сброс",
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
    "settings.group.session_idle_hours": "Автоархив через",
    "settings.idle_archive.body": (
        "*Автоархивация сессий*\n\n"
        "Через сколько часов без активности архивировать живую сессию. "
        "Архив остаётся доступен через Меню → Архив, сессию можно восстановить."
    ),
    "settings.value.hours": "{value} ч",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "выкл",
    "local.manual": "по кнопке",
    "local.auto": "всегда",
    "settings.group.card_history": "История в карточке",
    "settings.cardhist.body": (
        "*История в карточке*\n\n"
        "Сколько последних end-of-turn границ подгружать в карточку\n"
        "при первом доступе (после рестарта бота, тапа в свитчере или\n"
        "Меню → Sessions). Глубокая история сверх этого всегда\n"
        "доступна через /history независимо от значения.\n\n"
        "Больше = больше истории в карточке, больше памяти на сессию."
    ),
    "settings.group.card_page_lines": "Размер страницы",
    "settings.pagesize.body": (
        "*Размер страницы*\n\n"
        "Максимум строк на одну страницу карточки. Старые события\n"
        "уходят на предыдущие страницы (◀); длинный финальный ответ\n"
        "режется на несколько страниц по умным границам (абзац /\n"
        "строка / предложение / слово) — без обрывов посреди слова.\n"
        "Допускается отклонение ±5 строк.\n\n"
        "Меньше = компактнее для телефона. Больше = больше контекста\n"
        "на странице, но тяжелее edits."
    ),
    "settings.group.card_inline_screenshots": "Скрины в карточке",
    "settings.screens.body": (
        "*Скрины в карточке*\n\n"
        "Когда *on* — скрин терминала добавляется последним медиаблоком\n"
        "активной Rich Markdown-карточки: после ответов, tool-стейтов и\n"
        "спойлеров. Текст и скрин обновляются в одной live-карточке; после\n"
        "твоего следующего сообщения ниже появляется свежая карточка.\n"
        "Скрин обновляется только при изменении pane, не чаще ~раз в 3с.\n\n"
        "Когда *off* — остаётся обычный текстовый flow, а Shot доступен\n"
        "через кнопку терминала в верхнем ряду."
    ),
    "screens.on": "on",
    "screens.off": "off",
    "settings.group.bg_notify_finished": "Bg: задача готова",
    "settings.group.bg_notify_error": "Bg: ошибки",
    "settings.group.bg_notify_needs_action": "Bg: нужен ввод",
    "settings.bg_notify.finished.body": (
        "*Bg-сессия: задача готова*\n\n"
        "Когда фоновая сессия достигает end-of-turn, шлём тихий\n"
        "push ✅ [<name>] task complete, чтобы юзер мог переключиться."
    ),
    "settings.bg_notify.error.body": (
        "*Bg-сессия: ошибки*\n\n"
        "Push ❌ [<name>] error когда фоновая сессия эмитит\n"
        "ошибочный ивент. (Сейчас срабатывает только на явные\n"
        "error-ивенты; детект исключений будет расширен.)"
    ),
    "settings.bg_notify.needs_action.body": (
        "*Bg-сессия: нужен ввод*\n\n"
        "Push ❓ [<name>] needs your attention когда фоновая сессия\n"
        "показывает AskUserQuestion / ExitPlanMode / Permission промпт.\n"
        "Иначе только ❓ бейдж в bg-panel — легко пропустить."
    ),
    "settings.group.haiku_naming": "Имена сессий через AI",
    "settings.haiku.body": (
        "*Имена сессий через AI*\n\n"
        "При *on* каждая новая сессия переименовывается после первого\n"
        "пользовательского сообщения ≥20 символов одноразовым\n"
        "вызовом легковесной модели (Haiku для Claude,\n"
        "`CODEX_NAMING_MODEL` для Codex) — 1-3 слова в kebab-case о сути сессии\n"
        "(``token-budget-alerts``, ``archive-pagination-fix``).\n"
        "Сессии, переименованные вручную (``/rename``,\n"
        "``/new <name>``), никогда не перетираются.\n\n"
        "При *off* имя навсегда остаётся basename'ом директории\n"
        "(``workdir``, ``workdir-2``, ``ccbot``). Нулевой расход токенов."
    ),
    "settings.cat.card": "🃏 Карточка / вид",
    "settings.cat.notifications": "🔔 Уведомления",
    "settings.cat.voice": "🎙 Голос",
    "settings.cat.terminal": "🖥 Локальный терминал",
    "settings.cat.behavior": "⚙ Агент, поведение и язык",
    "settings.cat.card.body": (
        "*Карточка / вид*\n\nРаскладка, плотность и refresh живой карточки."
    ),
    "settings.cat.notifications.body": (
        "*Уведомления*\n\n"
        "Bg-сессионные пуши (готово / ошибки / нужен ввод) и день\n"
        "сброса для weekly-quota алертов."
    ),
    "settings.cat.voice.body": ("*Голос*\n\nДвижок speech-to-text для входящих voice."),
    "settings.cat.terminal.body": (
        "*Локальный терминал*\n\nНативное Terminal / iTerm окно к tmux."
    ),
    "settings.cat.behavior.body": (
        "*Поведение и язык*\n\n"
        "Глобальный агент; авто-Yes; имена через Haiku; язык интерфейса."
    ),
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
        "• *Idle TTL.* Автоархив через выбранные 6/12/24ч без активности.\n"
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
        "• ⚙ *Settings* — сгруппированы по Карточка / Уведомления / "
        "Голос / Терминал / Поведение."
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
        "Claude получает относительный путь (с caption-префиксом, если "
        "он был). TTL 24ч; Telegram `file_id` хранится 30д для "
        "`/restore-file`."
    ),
    "help.body.alerts": (
        "*Алерты*\n\n"
        "*Квоты Claude Code.* 5ч / неделя / неделя Sonnet — бот опрашивает "
        "живой `/usage` каждые 10 мин и пушит при пересечении 50, 75, 90 %.\n\n"
        "*Пуши по фоновым сессиям.* Settings → Уведомления, три "
        "независимых тумблера (все по умолчанию on):\n"
        "• ✅ task complete\n"
        "• ❌ error\n"
        "• ❓ needs your attention (интерактивный prompt)\n"
        "Активная сессия не пушит — она дописывает свою live-карточку.\n\n"
        "*Заполнение контекста.* На карточке у каждой сессии есть "
        "``context: N%``. Для Codex используются точные token usage и размер "
        "окна из rollout. Для Claude это оценка из JSONL: input + cache_read "
        "последнего assistant-turn относительно окна модели."
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
        "недоступен напрямую.\n"
        "• *Один инстанс.* Бот держит exclusive flock на "
        "`$CCBOT_DIR/ccbot.lock`; второй `uv run ccbot` откажется "
        "стартовать с ошибкой в stderr, не подерётся за Telegram updates.\n"
        "• *Self-heal хук.* `SessionStart` + `UserPromptSubmit` оба "
        "обновляют `session_map.json` — пропущенный SessionStart "
        "автоматически чинится при следующем prompt'е."
    ),
}

__all__ = ["RU"]
