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
  Telegram-id. Multi-tenant — out of scope.
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
| `BG_NOTIFY_MODE`            | `separate`   | `separate` (карточка на сессию) или `footer` |
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
`Shot`, `New`, `Archive`, `Settings`. Большинство пользователей вообще
не печатает слэш-команды, как только обнаруживают меню.

### Сессии и переключатель

Отправь любой текст в DM, чтобы создать первую сессию — бот откроет
браузер директорий, ты выберешь проект, в tmux-окне стартанёт
`claude`. Дальнейший текст в DM роутится в **активную** сессию.

В самом свежем сообщении бота — инлайн-переключатель (`▷ session-A
· session-B · + new`). Тап на неактивную сессию переключает активную
и показывает context-preview; тап на активную — no-op. Reply-цитата
на сообщение бота из неактивной сессии роутит твой текст туда разово,
без смены активной.

### Алерты по токенам

Два потока, оба отдельным push-уведомлением:

- **Per-session token alerts.** Три порога (по умолчанию
  100k/200k/400k, шаг 50k через Settings → Token alerts). Стреляет
  один раз на сессию на каждый порог.
- **5h / weekly / weekly-Sonnet quota alerts.** Фоновая задача
  опрашивает живой `/usage` каждые `QUOTA_ALERT_POLL_INTERVAL` и
  пушит, когда процент пересекает те же 50/75/90 %, что и stoplight-
  emoji.

### Голос и медиа

- **Голосовые сообщения** транскрибируются локально (whisper.cpp /
  Apple Speech) и попадают в активную сессию как набранный текст.
- **Фото и документы** ложатся в `<workdir>/.ccbot-inbox/`, Claude
  получает синтетическое сообщение через tmux. Файлы авто-чистятся
  через 24 часа.

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
