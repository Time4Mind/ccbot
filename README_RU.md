# ccbot

[![test](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml)
[![secrets-scan](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml)

[English README](README.md) · [中文文档](README_CN.md)

Личный Telegram-бот, соединяющий приватный 1-1 DM с N параллельными сессиями
Claude Code в tmux. Один пользователь, N сессий, инлайн-переключатель в
самом свежем сообщении бота.

## Зачем

Claude Code живёт в терминале. Отошёл от стола — потерял видимость, но
сессия продолжает работать. ccbot позволяет:

- **Переключаться с десктопа на телефон в середине работы.** Claude
  делает рефакторинг — идёшь на прогулку и продолжаешь следить и
  отвечать из Telegram.
- **Возвращаться на десктоп в любой момент.** Сессии живут в реальных
  tmux-окнах, `tmux attach` возвращает в терминал с полной историей.
- **Параллельно вести несколько сессий.** У каждой — своё tmux-окно
  и свой процесс `claude`. Переключение активной сессии в Telegram не
  ставит остальные на паузу.

Бот — тонкий слой управления над tmux: процесс Claude Code остаётся
ровно там, где был. ccbot читает его вывод и отправляет нажатия
клавиш.

## Отличия от upstream

Этот форк (`feat/dm-multisession`) сознательно расходится с upstream
`ccbot` в нескольких принципиальных моментах:

- **Только DM.** Никаких супергрупп, форум-топиков, thread-routing'а.
  Бот видит исключительно приватный 1-1 чат с одним allowlisted
  Telegram-юзером.
- **Один пользователь.** В `ALLOWED_USERS` ровно один числовой
  Telegram-id. Multi-tenant — out of scope. Любое сообщение от не-
  allowlisted отправителя молча отбрасывается (без ответа, без
  callback-тоста) — для постороннего бот выглядит мёртвым.
- **Только bypass-режим.** `claude` запускается с
  `--dangerously-skip-permissions`. Релэя permission-промптов в
  Telegram нет — если не доверяешь модели полный доступ к хосту,
  используй upstream.
- **Multi-session с инлайн-переключателем.** В одном DM может жить
  много сессий; инлайн-клавиатура под последним сообщением бота
  переключает между ними.
- **MarkdownV2** в pipeline'е (через `telegramify-markdown`) с авто-
  фолбэком на plain text при ошибке парсинга. В upstream — HTML.
- **Hook-based session tracking.** `SessionStart` хук Claude Code
  пишет `session_map.json`; монитор бота его опрашивает. Никаких
  process-tree introspection или claude SDK.
- **Голос — local-first.** `whisper.cpp` (по умолчанию) или Apple
  Speech через PyObjC на macOS. OpenAI-fallback есть, но выключен —
  API-ключ для запуска не нужен.

Полная архитектурная мотивация — в `doc/dm-multisession-spec.md`.
Карта реализации — в `doc/dm-multisession-plan.md`.

## Требования

- **tmux** в `PATH`
- **Claude Code** CLI (`claude`) с активным Max-аккаунтом
- **Python 3.12+**
- **uv** (рекомендуется) для управления зависимостями
- macOS (Apple Silicon) или Linux arm64

Опционально:

- **`ffmpeg`** + **`whisper-cli`** для локальной голосовой транскрипции
- **`pyobjc-framework-Speech`** для нативного Apple Speech-бэкенда
  (`uv sync --extra apple-speech`)

## Быстрый старт

```bash
git clone https://github.com/Time4Mind/ccbot.git
cd ccbot
uv sync
cp .env.example ~/.ccbot/.env   # вписать TELEGRAM_BOT_TOKEN + ALLOWED_USERS
ccbot hook --install            # один раз: регистрация Claude Code SessionStart-хука
ccbot                           # foreground; для prod — systemd-юнит
```

## Конфигурация

Обязательные env-переменные в `~/.ccbot/.env` (или `./.env`):

| Переменная           | Описание                                        |
| -------------------- | ----------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Токен от [@BotFather](https://t.me/BotFather)   |
| `ALLOWED_USERS`      | Один числовой Telegram-user-id                  |

Чаще всего настраиваемые опциональные:

| Переменная                  | По умолчанию | Эффект |
| --------------------------- | ------------ | ------ |
| `CCBOT_DIR`                 | `~/.ccbot`   | Каталог конфигов и состояния |
| `TMUX_SESSION_NAME`         | `ccbot`      | tmux-сессия, где живут все session-окна |
| `CLAUDE_COMMAND`            | `claude`     | бинарь для старта сессии |
| `CLAUDE_FLAGS`              | `--dangerously-skip-permissions` | флаги для `claude` |
| `SESSION_IDLE_TTL`          | `4h`         | active → archived через столько простоя |
| `ARCHIVE_PURGE_AFTER`       | `14d`        | архивные сессии удаляются из state через столько |
| `QUOTA_ALERT_POLL_INTERVAL` | `5m`         | как часто опрашивается живой `/usage` |
| `VOICE_BACKEND`             | `auto`       | `auto` / `whisper` / `apple` / `off` |
| `WHISPER_MODEL_PATH`        | `~/.ccbot/models/ggml-medium.bin` | модель whisper.cpp |
| `BG_NOTIFY_MODE`            | `separate`   | _(legacy)_ оставлен для совместимости; фоновые сессии теперь молчат — см. «Фоновые сессии» |
| `BG_STATUS_MAX`             | `4`          | макс. бейджей в bg-status-панели; остальные сворачиваются в `+N more` |
| `BG_STATUS_QUOTA_THRESHOLDS`| `100000,200000,400000` | пороги токенов сессии, переключающие глиф ⚠️🟢/🟡/🔴 |
| `CARD_PRIOR_CONTEXT`        | `5`          | сколько событий транскрипта подгружать в шапку свежей live-карточки; `0` отключает |
| `CARD_EDIT_LAG`             | `2.0`        | окно коалесцинга редактирований карточки (секунды) |
| `TG_PROXY_URL`              | _(не задан)_ | outbound-прокси для Bot API (`socks5://…` или `http://…`) |

Полный список — в `doc/dm-multisession-spec.md` § 14.

## Установка хука

Бот трекает связки tmux-окно → Claude-session через `SessionStart`-хук
Claude Code. Авто-установка одной командой:

```bash
ccbot hook --install
```

Или вручную в `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ]
  }
}
```

## Использование

Бот публикует небольшое slash-меню в `/`-меню Telegram плюс инлайн-
кнопку `≡ Меню` под самым свежим сообщением:

| Команда   | Эффект |
| --------- | ------ |
| `/menu`   | Открыть инлайн ≡ Меню |
| `/help`   | Краткий гайд (раздельные секции с инлайн-навигацией) |
| `/health` | Uptime, состояние tmux, очереди, latency, счётчики |
| `/done`   | Закрыть активную сессию (архивировать как «выполнено») |

Остальные действия — за инлайн-меню: `List`, `Status`, `History`,
`New`, `Archive`, `Settings`. Кнопка 🧑‍💻 *Shot* (скриншот терминала)
живёт в управляющем ряду главного экрана и в *Меню → List* — рядом с
*Kill* и *Clear* — чтобы быть всегда под рукой на самой transcript-
поверхности. Большинство пользователей вообще не печатает слэш-команды,
как только обнаруживают меню.

### Сессии и переключатель

Отправь любой текст в DM, чтобы создать первую сессию — бот откроет
браузер директорий, ты выберешь проект, в tmux-окне стартанёт
`claude`. Дальнейший текст в DM роутится в **активную** сессию.

В самом свежем сообщении бота — инлайн-переключатель
(`▷ session-A · session-B`), а внизу — парный ряд `[+ new] [≡ Меню]`:
две кнопки «уйти отсюда» рядом друг с другом, слот стабилен между
видами (в *Меню → List* / *Archive* этот же слот занимает
`[+ new] [Back]`).

Тап на неактивную сессию **рисует полную историю транскрипта** этой
сессии прямо на carrier-сообщении и одновременно переключает
активную — без дополнительного `≡ Меню → История`. Кнопки пагинации
(◀ Older / Newer ▶) сохраняют футер под ними. Тап на уже активную —
no-op.

Reply-цитата на сообщение бота из неактивной сессии роутит твой
текст туда разово, без смены активной.

*Меню → Archive* показывает пронумерованный список прошлых сессий
по две кнопки в ряд. У каждой строки — короткое описание (Claude'овый
`type=summary` или первое сообщение пользователя), чтобы сразу было
понятно, о чём была сессия. Тап по сессии — carrier рисует реальный
transcript прямо с диска (JSONL); *Restore* / *Delete* остаются в
футере.

### Фоновые сессии

Фоновые (неактивные) сессии **молчат в чате** — никаких правок live-
card, push-уведомлений, AskUserQuestion-промптов. Их состояние
проявляется только в компактной панели внизу карточки активной
сессии:

```
[session-A] ⏳         ← работает в фоне
[scraper]   ✅         ← завершилась
[chores]    ❌         ← упала
[v frontend] ❓ ⚠️🟡    ← требует действия + перешла порог токенов
```

Панель «липкая» поверх редактирований активной карточки, чтобы
завершившаяся фоновая сессия не потерялась над длинным tool-логом.
Тап по бейджу (через переключатель) убирает его из панели — ты
«увидел». Если бейдж `❓`, тап рисует сохранённый AskUserQuestion /
ExitPlanMode-промпт с теми же стрелками/Enter/Esc, что и на
foreground-промпте.

Когда сессия пересекает один из порогов `BG_STATUS_QUOTA_THRESHOLDS`,
её квотный глиф переключается на `⚠️🟢` / `🟡` / `🔴`. Активная
сессия показывает тот же глиф в шапке. **Никаких push-уведомлений
не шлётся** — индикатор и есть алерт.

### UX-настройки живой карточки

Свежая live-card (после завершения турна / clear / overflow) пред-
заполняется до `CARD_PRIOR_CONTEXT` (по умолчанию 5) записей
транскрипта перед твоим последним сообщением, чтобы контекст не
терялся между турнами.

`Settings → Card position` управляет тем, как твой исходящий текст
соотносится с live-card:
- `push` — оставить как есть (твоё сообщение сдвигает карточку
  вверх; по умолчанию)
- `delete` — бот удаляет твоё сообщение, карточка остаётся
  последней
- `repost` — бот пересылает карточку под твоё сообщение и удаляет
  старую

Индикатор Telegram **`печатает…`** в шапке чата управляется
реальными событиями claude. Пока активная сессия эмитит (tool-call,
thinking, текст) — `печатает…` горит; idle-сессия даёт ему погаснуть
в течение ~5 с (Telegram-окно).

### Голос и медиа

- **Голосовые сообщения** транскрибируются локально (whisper.cpp /
  Apple Speech) и попадают в активную сессию как набранный текст.
  Reply'ем бот эхает транскрипцию, чтобы ты видел, что получил
  Claude.
- **Фото и документы** ложатся в `<workdir>/.ccbot-inbox/`, Claude
  получает синтетическое сообщение через tmux. Файлы авто-чистятся
  через 24 часа.
- **Пересланные посты с медиа** (channel-posts с video / GIF /
  sticker и текстом-caption) — caption + скрытые `text_link`-URL
  извлекаются и роутятся в активную сессию с префиксом
  `[forwarded from @channel]`. Сам медиа-payload отбрасывается —
  Claude его не съест.

## Архитектура

Полная карта модулей — `.claude/rules/architecture.md`. Кратко:

```
src/ccbot/
├── main.py                 — CLI entry point (`ccbot`, `ccbot hook`)
├── config.py               — загрузчик env-vars (singleton)
├── session.py              — Session + SessionManager (state.json)
├── session_monitor.py      — JSONL polling, NewMessage callbacks
├── transcript_parser.py    — парсинг JSONL-турнов
├── terminal_parser.py      — детект interactive UI + status line
├── tmux_manager.py         — обёртка над libtmux
├── markdown_v2.py          — MD → Telegram MarkdownV2
├── telegram_sender.py      — split_message по 4096-char лимиту
├── transcribe.py           — voice → text диспетчер
├── usage.py                — токен-агрегатор + alert-логика
├── i18n.py                 — UI-строки en / ru / zh
├── bot/                    — Telegram-handlers (≤ 600 LOC на файл)
│   ├── app.py              — bootstrap, post_init / post_shutdown
│   ├── messages.py         — text / voice / photo / document / forward
│   ├── session_events.py   — claude → TG dispatch
│   ├── commands/           — тела slash-команд
│   └── callbacks/          — по файлу на CB_* префикс
└── handlers/
    ├── notifications.py    — live cards + push events
    ├── archive.py          — /archive рендер + idle sweeps
    ├── quota_alerts.py     — фоновый /usage poll
    ├── interactive_ui.py   — AskUserQuestion / ExitPlanMode
    ├── menu.py             — компоновка инлайн-клавиатур
    └── …
```

Состояние — в `$CCBOT_DIR` (по умолчанию `~/.ccbot/`):

| Файл                | Содержимое |
| ------------------- | ---------- |
| `state.json`        | сессии, active_sessions, window states, user settings |
| `session_map.json`  | hook-генерируемая tmux-window → claude-session карта |
| `monitor_state.json`| per-JSONL byte offsets (защита от дублей при рестарте) |

## Развёртывание

systemd-юнит лежит в `scripts/ccbot.service`. Для VPS-хостов без
прямого доступа к `api.telegram.org` — SSH-tunnel-рецепт через
`TG_PROXY_URL` в `doc/deploy.md`. Полная пошаговая Linux-инсталляция
(для AI-агента) — `doc/install-linux.md`.

## Контрибуция

См. [CONTRIBUTING.md](CONTRIBUTING.md). Кратко: PR'ы, согласующиеся с
инвариантами DM-only / single-user / bypass-only — welcome. CI должен
быть зелёным; pre-commit-хуки должны пройти; один PR — одна цель.

## Безопасность

См. [SECURITY.md](SECURITY.md): threat model и процесс репорта.
Уязвимости — через GitHub Security Advisories, не публичные issue.

## Лицензия

См. [LICENSE](LICENSE).
