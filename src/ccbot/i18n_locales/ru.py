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
    "btn.kill": "✕ Закрыть",
    "btn.menu": "≡ Меню",
    "btn.options": "⋯ Опции",
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
    "btn.no": "× Нет",
    "btn.yes_kill": "⚠ Да, закрыть",
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
    "mm.sessions": "📋 Сессии",
    "mm.status": "📊 Статус",
    "mm.history": "📜 История",
    "mm.shot": "🧑‍💻 Скрин",
    "mm.new": "🆕 Новая",
    "mm.archive": "🗄 Архив",
    "mm.settings": "⚙ Настройки",
    "mm.nodes": "🖧 Ноды",
    "mm.transfer": "⇄ Перенос",
    "menu.title": "*Меню*",
    "menu.empty": "*Меню*\n\nАктивной сессии нет — выбери в свитчере или тапни 🆕 Новая.",
    "menu.active": "*Меню* · активна: *{name}*",
    "settings.title": "*Настройки*",
    "settings.table.section": "Раздел",
    "settings.table.contents": "Настройки",
    "settings.table.setting": "Настройка",
    "settings.table.current": "Текущее значение",
    "settings.table.button": "Кнопка",
    "settings.table.show": "Показывать",
    "settings.table.hint": "_Тапни раздел, чтобы открыть._",
    "settings.body": (
        "*Настройки*\n\n"
        "Агент: `{agent}`\n"
        "Язык: `{language}`\n"
        "Лаг карточки: `{live_lag}с`\n"
        "Голос: `{voice}`\n\n"
        "_Тапни группу, чтобы изменить._"
    ),
    "settings.group.agent": "Агент",
    "settings.group.default_session": "Резервная сессия",
    "settings.group.default_directory": "Директория резерва",
    "settings.cat.sessions": "Сессии и бэкенды",
    "settings.cat.sessions.body": "*Сессии и бэкенды*",
    "settings.default_session.body": (
        "*Резервная сессия*\n\nВсегда держит одну пустую сессию готовой "
        "в выбранной директории. После первого запроса сразу создаётся новая."
    ),
    "settings.default_directory.body": "*Директория резервной сессии*",
    "settings.default_directory.choose": "📁 Выбрать директорию",
    "settings.default_directory.invalid": "⚠ Директория недоступна - резерв не запущен.",
    "toast.default_directory_required": "Сначала выбери директорию резерва.",
    "toast.last_backend": "Последний активный бэкенд выключить нельзя.",
    "settings.group.language": "Язык",
    "settings.group.live_lag": "Лаг карточки",
    "settings.group.voice": "Голос",
    "settings.lag.body": (
        "*Лаг карточки*\n\n"
        "Окно сглаживания правок live-карточки.\n"
        "Любое выбранное значение растёт в `1.5/2.5/5 раз` "
        "после `15/35/65 мин` тишины."
    ),
    "settings.voice.body": (
        "*Распознавание голоса*\n\n"
        "Бэкенд для voice-сообщений.\n"
        "• `auto` — Parakeet (та же локальная модель, что в Bria)\n"
        "• `parakeet` — форсить Parakeet через NeMo-Speech.cpp\n"
        "• `whisper` — форсить whisper.cpp\n"
        "• `apple` — форсить Apple Speech (только macOS)\n"
        "• `off` — игнорировать voice"
    ),
    "settings.agent.body": (
        "*Агент*\n\n"
        "✅ включает бэкенд. Второй ряд выбирает бэкенд по умолчанию для "
        "резерва. Если включены оба, новая сессия сначала спросит бэкенд."
    ),
    "settings.lang.body": (
        "*Язык*\n\nЯзык интерфейса. Переключает всё,\nкроме самого вывода Claude."
    ),
    "list.empty": "Активных сессий нет. Тапни 🆕 Новая, чтобы создать.",
    "conf.kill": (
        "Убить *{name}*?\nTmux-окно умрёт, claude session id сохранится.\n"
        "Восстановить можно через архив."
    ),
    "conf.delete": (
        "Удалить *{name}* из архива?\nЗапись стирается. JSONL остаётся на диске."
    ),
    "conf.clear": (
        "Очистить *{name}*?\nОтправит Esc, затем /clear. Контекст сессии "
        "стирается без возможности восстановления (в отличие от Kill → Restore)."
    ),
    "conf.killed": "💀 Убита `{name}`",
    "conf.deleted": "🗑 Запись из архива удалена.",
    "dir.title": "*Выбор рабочей директории*",
    "backend.choose": "*Выбери бэкенд*",
    "dir.current": "Текущая: `{path}`",
    "dir.empty": "_(Поддиректорий нет)_",
    "dir.hint": "Тапни папку, чтобы войти, или выбери текущую",
    "dir.btn.up": "..",
    "dir.btn.select": "Выбрать",
    "dir.btn.create": "Создать папку",
    "dir.create.prompt": "{path}\n\nВведи имя новой папки без слешей.",
    "dir.create.created": "Папка создана.",
    "dir.create.exists": "Папка уже существует - открываю её.",
    "dir.create.failed": "Не удалось создать папку.",
    "picker.title": "*Возобновить сессию?*",
    "picker.summary": "стр. {page}/{pages} — {total} сессий в этой папке.",
    "picker.btn.start_fresh": "🆕 С нуля",
    "picker.btn.back_to_dirs": "← К папкам",
    "toast.no_session": "Нет активной сессии",
    "toast.window_gone": "Окно исчезло",
    "toast.esc_sent": "⎋ Esc отправлен",
    "toast.cleared": "🧹 Контекст очищен",
    "toast.killed": "Убита",
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
    # Archive screen
    "archive.title": "Архивные сессии",
    "archive.empty": "Архивных сессий в этом окне нет.",
    "archive.page_line": "стр. {page}/{pages} — всего {total}",
    "archive.age.s": "{n}с",
    "archive.age.m": "{n}м",
    "archive.age.h": "{n}ч",
    "archive.age.d": "{n}д",
    "archive.column.session": "Сессия",
    "archive.column.description": "Описание",
    "archive.link.yql": "ссылка в YQL",
    "archive.link.tracker": "ссылка в Tracker",
    "archive.link.github": "ссылка в GitHub",
    "archive.link.arcanum": "ссылка в Arcanum",
    "archive.link.wiki": "ссылка в Wiki",
    "archive.link.generic": "ссылка: {host}",
    "usage.title": "*Claude Code*",
    "usage.title.codex": "*OpenAI Codex*",
    "usage.status_age": "Статус · {age}м",
    "usage.column.cli": "CLI",
    "usage.column.5h": "5 часов",
    "usage.column.week": "Неделя",
    "usage.column.today": "Сегодня",
    "usage.column.reset": "Сброс",
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
    "settings.group.card_history": "История в карточке",
    "settings.cardhist.body": (
        "*История в карточке*\n\n"
        "Сколько последних end-of-turn границ подгружать в карточку\n"
        "при первом доступе (после рестарта бота, тапа в свитчере или\n"
        "Меню → Sessions).\n\n"
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
    "settings.group.spoiler_command_lines": "Строк команды",
    "settings.spoiler_command_lines.body": (
        "*Строки команды*\n\n"
        "Максимум видимых строк команды в спойлере. Одна строка ограничена "
        "100 символами."
    ),
    "settings.group.spoiler_result_lines": "Строк результата",
    "settings.spoiler_result_lines.body": (
        "*Строки результата*\n\n"
        "Максимум видимых строк результата в спойлере. Одна строка ограничена "
        "100 символами."
    ),
    "screens.on": "on",
    "screens.off": "off",
    "settings.group.bg_notify_finished": "Bg: задача готова",
    "settings.group.bg_notify_error": "Bg: ошибки",
    "settings.group.bg_notify_needs_action": "Bg: нужен ввод",
    "settings.group.bg_notify_node_status": "Ноды: связь",
    "settings.bg_notify.node_status.body": (
        "*Состояние нод*\n\nУведомлять, если выбранная нода или нода с активной "
        "сессией недоступна больше минуты, и один раз после восстановления. "
        "Краткие сетевые сбои остаются без уведомлений."
    ),
    "settings.bg_notify.finished.body": (
        "*Bg-сессия: задача готова*\n\n"
        "Когда фоновая сессия достигает end-of-turn, шлём тихий\n"
        "push ✅ [<name>] task complete, чтобы юзер мог переключиться."
    ),
    "settings.bg_notify.error.body": (
        "*Bg-сессия: ошибки*\n\n"
        "Push ❗ [<name>] error один раз при переходе фоновой "
        "сессии в блокирующую ошибку."
    ),
    "settings.bg_notify.needs_action.body": (
        "*Bg-сессия: нужен ввод*\n\n"
        "Push ❗ [<name>] needs your attention когда фоновая сессия\n"
        "ждет ввода или ручного подтверждения."
    ),
    "settings.group.haiku_naming": "Имена сессий через AI",
    "settings.group.archive_ai_description": "AI-описание",
    "settings.archive_ai_description.body": (
        "*AI-описание*\n\nПри *on* легковесная модель формулирует одно короткое "
        "описание архива по первым двум запросам. При *off* показываются сами "
        "два запроса, каждый начинается с ·."
    ),
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
    "settings.cat.options": "⋯ Кнопки опций",
    "settings.cat.preprocessing": "💻 Препроцессинг",
    "settings.cat.behavior": "⚙ Поведение и язык",
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
    "settings.cat.options.body": "*Кнопки опций*\n\nКакие действия показывать под кнопкой Опции.",
    "settings.group.option_button_transfer": "Перенос сессии",
    "settings.option_button_transfer.body": (
        "*Перенос сессии*\n\nПоказывать кнопку переноса контекста на другую ноду. "
        "Кнопка появится только при наличии нескольких нод и в простой сессии."
    ),
    "nodes.title": "*Ноды*",
    "nodes.empty": "Зарегистрированных нод нет.",
    "nodes.table.node": "Нода",
    "nodes.table.state": "Состояние",
    "nodes.table.backends": "Бэкенды",
    "nodes.state.pending": "подключается",
    "nodes.state.online": "онлайн",
    "nodes.state.offline": "оффлайн",
    "nodes.state.ready": "готова",
    "nodes.disable": "Отключить",
    "nodes.enable": "Подключить",
    "nodes.disable.done": "Нода отключена. Сессии и история сохранены.",
    "nodes.enable.done": "Нода снова доступна для работы.",
    "nodes.delete.confirm": (
        "*Удалить ноду из ccbot?*\n\nНода: *{node}*\n\n"
        "Сессии, transcript и архивы не удаляются."
    ),
    "nodes.delete.done": "Нода удалена из реестра ccbot.",
    "nodes.delete.not_found": "Нода не найдена или уже удалена.",
    "nodes.delete.revoke_failed": "Не удалось отозвать доступ ноды. Удаление отменено.",
    "transfer.choose_node": "*Перенос сессии*\n\nСессия: *{session}*\n\nВыбери целевую ноду.",
    "transfer.choose_backend": "*Выбери бэкенд*\n\nЦелевая нода: *{node}*",
    "transfer.confirm": (
        "*Подтвердить перенос*\n\nСессия: *{session}*\n"
        "Целевая нода: *{node}*\nБэкенд: *{backend}*\n\n"
        "Переносится только контекст сессии."
    ),
    "transfer.btn.start": "✅ Перенести",
    "transfer.started": "⏳ Перенос запущен. Новые запросы будут поставлены в очередь до готовности целевой сессии.",
    "transfer.ready": "✅ Целевая сессия готова.",
    "transfer.cancelled": "Перенос отменён.",
    "transfer.unavailable": "Перенос сейчас недоступен.",
    "transfer.target_unavailable": "Целевая нода недоступна.",
    "transfer.backend_unavailable": "Этот бэкенд недоступен на целевой ноде.",
    "transfer.failed": "❌ Не удалось перенести сессию: {error}",
    "transfer.context_limit": (
        "⚠ Целевая сессия создана, но весь контекст не поместился.\n"
        "Причина: {error}\n\nПолный контекст: `{path}`"
    ),
    "settings.cat.preprocessing.body": (
        "*Препроцессинг*\n\nКонсервативная подготовка запроса общей Luna перед отправкой в целевую сессию."
    ),
    "settings.group.preprocessing_mode": "Режим",
    "settings.group.preprocessing_instruction": "Инструкция",
    "settings.preprocessing_mode.body": (
        "*Режим препроцессинга*\n\nВыключен, только голос или все текстовые и голосовые запросы."
    ),
    "settings.preprocessing_instruction.body": (
        "*Инструкция препроцессинга*\n\nПустое значение использует встроенную консервативную инструкцию."
    ),
    "preprocessing.mode.off": "Выключен",
    "preprocessing.mode.voice": "Только голос",
    "preprocessing.mode.all": "Все запросы",
    "preprocessing.instruction.builtin": "Встроенная",
    "preprocessing.instruction.custom": "Пользовательская",
    "preprocessing.instruction.edit": "Изменить",
    "preprocessing.instruction.reset": "Вернуть встроенную",
    "preprocessing.instruction.send": "Отправь новую инструкцию одним текстовым сообщением.",
    "preprocessing.instruction.invalid": "Инструкция должна быть непустой и не длиннее 16 KiB.",
    "settings.group.option_button_screenshot": "🧑‍💻 Скрин",
    "settings.group.option_button_terminal": "🖥 Терминал",
    "settings.option_button_screenshot.body": "*Кнопка Скрин*\n\nПоказывать Скрин в Опциях.",
    "settings.option_button_terminal.body": "*Кнопка Терминал*\n\nПоказывать Терминал в Опциях, когда он доступен.",
    "settings.group.screenshot_capture_kib": "Объём скрина",
    "settings.screenshot_capture.body": "*Объём скрина*\n\nМаксимальный объём свежего хвоста терминального текста для изображения.",
    "settings.group.screenshot_profile": "Качество скрина",
    "settings.screenshot_profile.body": "*Качество скрина*\n\nМасштаб и цветовой профиль изображения.",
    "screenshot.profile.full8": "100%, оптимизировано",
    "screenshot.profile.compact8": "75%, оптимизировано",
    "screenshot.profile.fullcolor": "100%, полная палитра",
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
        "появляется в ряду под *⋯ Опции*\n"
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
        "• *Закрыть.* Кнопка «Закрыть» под карточкой архивирует сессию.\n"
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
        "• `/health` — uptime, очереди, latency, счётчики\n\n"
        "Claude Code (форвардятся как есть):\n"
        "• `/model` `/clear` `/cost` …\n\n"
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
