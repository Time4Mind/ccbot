# ccbot — установка на Linux (инструкция для агента)

Пошаговая инструкция для AI-агента (например Claude Code), который
разворачивает ccbot на чистом Linux-сервере. Цель — поднять бот так,
чтобы:

1. systemd-юнит автоматически стартовал tmux + `ccbot` на boot;
2. в Telegram-DM работали все слэш-команды и инлайн-меню;
3. SessionStart-хук Claude Code был установлен (для трекинга
   `session_id ↔ tmux window`);
4. голос/медиа работали (whisper.cpp опционально).

Целевая платформа: **Linux x86_64 или arm64**, дистрибутивы на базе
Debian/Ubuntu (для других — адаптировать пакетный менеджер). Работа
ведётся под обычным пользователем с правом `sudo`.

---

## 0. Перед началом — что должен знать агент

Прочитай эти файлы из репозитория, прежде чем что-либо менять
снаружи:

- `CLAUDE.md` — обзор и правила проекта
- `doc/dm-multisession-spec.md` — продуктовая спека (env-vars, UX)
- `doc/dm-multisession-plan.md` — карта реализации
- `.claude/rules/secrets.md` — где живут токены (НЕ в репо)
- `scripts/ccbot.service` — шаблон systemd-юнита
- `.env.example` — список переменных окружения

Секреты (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`) хранятся **только** в
`~/.ccbot/.env` или `/etc/ccbot/ccbot.env`. Никогда не коммить их и
не клади в `CLAUDE.md`.

---

## 1. Собери входные данные у пользователя

Прежде чем выполнять команды, агент **обязан** получить от
пользователя:

| Параметр                 | Пример                       | Где взять |
|--------------------------|------------------------------|-----------|
| `TELEGRAM_BOT_TOKEN`     | `123456:ABC-DEF...`          | [@BotFather](https://t.me/BotFather) → `/newbot` |
| `ALLOWED_USERS`          | `123456789` (свой числовой id) | [@userinfobot](https://t.me/userinfobot) |
| Каталог установки        | `/opt/ccbot` (рекомендация) | — |
| Системный пользователь   | текущий `$USER`              | под ним работает Claude Code |
| Нужен ли whisper-голос?  | да/нет                       | при «нет» этап 6 пропускается |
| Нужен ли исходящий прокси?| `socks5://...` или нет      | если бот блокируется в стране |

Если что-то из обязательного не предоставлено — **остановись и
спроси**, не пытайся угадать.

---

## 2. Системные пакеты

```bash
sudo apt update
sudo apt install -y \
    tmux \
    git \
    curl \
    ca-certificates \
    build-essential \
    ffmpeg
```

Проверка:

```bash
tmux -V          # tmux 3.x
ffmpeg -version  # любой
```

`ffmpeg` нужен для голосовых: PTT-сообщения Telegram приходят как
`.ogg/opus`, бот декодирует их перед whisper-cli.

---

## 3. Python 3.12 + uv

ccbot требует **Python ≥ 3.12**. На Ubuntu 24.04 он уже есть. На
Ubuntu 22.04 — поставь через deadsnakes:

```bash
# Только если в системе нет python3.12
if ! command -v python3.12 >/dev/null; then
    sudo add-apt-repository -y ppa:deadsnakes/ppa
    sudo apt update
    sudo apt install -y python3.12 python3.12-venv
fi
python3.12 --version
```

Установи `uv` (агент: ставь под того же пользователя, под которым
будет работать systemd-юнит):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# uv ставится в ~/.local/bin — должен быть в PATH
export PATH="$HOME/.local/bin:$PATH"
uv --version
```

Если `uv` ставится в другое место — **перед записью systemd-юнита
зафиксируй путь** через `command -v uv` и подставь его в `ExecStart`
вместо `/usr/bin/env uv`.

---

## 4. Claude Code CLI

ccbot — это лишь мост к `claude` CLI. Без него ничего не запустится.

```bash
# Официальный установщик; ставит в ~/.claude/local/
curl -fsSL https://claude.ai/install.sh | bash
export PATH="$HOME/.claude/local:$PATH"
claude --version
```

Логин через подписку (Max). **Шаг интерактивный** — агент не должен
выполнять его сам, а должен попросить пользователя:

> Запусти у себя в терминале команду ниже и пройди OAuth в браузере:
> ```
> claude login
> ```

После логина проверь:

```bash
claude auth status   # должен показать активную подписку
```

`ANTHROPIC_API_KEY` **НЕ нужен** — авторизация через CLI subscription.

---

## 5. Клонирование и сборка ccbot

```bash
sudo mkdir -p /opt
sudo chown "$USER:$USER" /opt
cd /opt
git clone https://github.com/Time4Mind/ccbot.git
cd ccbot
# DM-режим живёт в main; отдельной ветки больше нет.
uv sync                              # создаст .venv и поставит зависимости
```

Sanity-check (агент должен прогнать перед коммитом установки):

```bash
uv run ruff check src/ tests/
uv run pyright src/ccbot/
```

Если что-то падает — **не пытайся «починить» правкой кода**, это уже
не задача установки. Сообщи пользователю и остановись.

---

## 6. Конфиг (`~/.ccbot/.env`)

```bash
mkdir -p ~/.ccbot
cp /opt/ccbot/.env.example ~/.ccbot/.env
chmod 600 ~/.ccbot/.env
```

Открой `~/.ccbot/.env` и заполни **минимум два поля**:

```ini
TELEGRAM_BOT_TOKEN=<значение от пользователя>
ALLOWED_USERS=<один числовой Telegram-id>
```

Опционально (по запросу пользователя):

```ini
TG_PROXY_URL=socks5://user:pass@host:1080   # если бот в блок-стране
VOICE_BACKEND=whisper                        # см. шаг 7
WHISPER_MODEL_PATH=/home/USER/.ccbot/models/ggml-medium.bin
```

**Не пиши** токен в `/opt/ccbot/.env`, в `CLAUDE.md`, в логи или в
этот md-файл. Только `~/.ccbot/.env` (mode 600) или
`/etc/ccbot/ccbot.env` (mode 640, owner=root, group=ccbot).

---

## 7. Голосовой бэкенд (опционально)

Пропусти этот раздел, если пользователь сказал «без голоса». Тогда в
`.env` поставь `VOICE_BACKEND=off`.

Иначе — поставь whisper.cpp и скачай модель:

```bash
# whisper-cli — биндинг whisper.cpp; в Debian/Ubuntu обычно нет в apt,
# собери из исходников или возьми готовый бинарь.
sudo apt install -y cmake
git clone https://github.com/ggerganov/whisper.cpp.git /tmp/whisper.cpp
cd /tmp/whisper.cpp
cmake -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j
sudo install -m 0755 build/bin/whisper-cli /usr/local/bin/whisper-cli
whisper-cli --help | head -5
```

Скачай модель через скрипт репозитория (medium ~1.5 GB, multilingual
+ русский):

```bash
cd /opt/ccbot
MODEL=medium ./scripts/install_whisper_model.sh
```

Скрипт сам положит файл в `~/.ccbot/models/ggml-medium.bin` и
напечатает строки для `.env`. Если у пользователя медленный канал —
предложи `MODEL=small` (~488 MB).

В `.env` добавь:

```ini
VOICE_BACKEND=whisper
WHISPER_MODEL_PATH=/home/USER/.ccbot/models/ggml-medium.bin
```

---

## 8. SessionStart hook

Хук пишет `~/.ccbot/session_map.json` при каждом старте/ресуме claude
— без него бот не сможет связать tmux-окно с claude-сессией.

```bash
cd /opt/ccbot
uv run ccbot hook --install
```

Проверка: в `~/.claude/settings.json` должен появиться блок
`hooks.SessionStart` с `command: "ccbot hook"`.

---

## 9. Smoke-test (foreground)

Прежде чем оборачивать в systemd, убедись, что бот стартует
руками. Используй tmux-сессию `ccbot` — её ожидает `restart.sh`:

```bash
tmux new-session -d -s ccbot -n __main__
tmux send-keys -t ccbot:__main__ "cd /opt/ccbot && uv run ccbot" Enter
sleep 5
tmux capture-pane -t ccbot:__main__ -p | tail -30
```

Что должно быть в выводе:

- строка про загрузку конфига из `~/.ccbot/.env`;
- `Bot started`/`Application started polling`;
- никаких трейсбеков.

В Telegram отправь боту любое сообщение — он должен открыть
directory-browser и предложить выбрать каталог для первой сессии.

Останови smoke-test перед переходом к шагу 10:

```bash
tmux send-keys -t ccbot:__main__ C-c
sleep 2
tmux kill-session -t ccbot
```

---

## 10. systemd-юнит (постоянная работа)

Шаблон лежит в `scripts/ccbot.service`. Подставь имя пользователя и
скопируй:

```bash
sudo install -d -m 0755 /etc/systemd/system
sudo install -m 0644 /opt/ccbot/scripts/ccbot.service \
    /etc/systemd/system/ccbot@.service

# Опционально: общий env-файл (альтернатива ~/.ccbot/.env)
sudo install -d -m 0750 /etc/ccbot
# Если используешь — sudo cp ... /etc/ccbot/ccbot.env && sudo chmod 640

sudo systemctl daemon-reload
sudo systemctl enable --now "ccbot@$USER.service"
```

Юнит — template (`@`); параметр после `@` это имя Linux-пользователя,
под которым запускается бот (`User=%i` в шаблоне). Для пользователя
`art` команда будет `ccbot@art.service`.

Проверка:

```bash
systemctl status "ccbot@$USER.service" --no-pager
journalctl -u "ccbot@$USER.service" -n 50 --no-pager
```

В журнале должны быть те же строки, что и в smoke-test.

Если на хосте нет systemd (chroot, контейнер без init, и т.п.), смотри
`doc/deploy.md` → «Без systemd» — там описан platform-agnostic
supervisor с авто-рестартом и ожиданием сети.

---

## 11. Проверка end-to-end

В Telegram (под аккаунтом из `ALLOWED_USERS`):

1. `/start` → бот отвечает приветствием.
2. Любой текст → открывается directory-browser, выбираешь каталог.
3. После выбора создаётся tmux-окно с `claude`, в чат приходит
   первое сообщение от модели.
4. `/list` → видна одна активная сессия.
5. `/menu` → открывается инлайн-меню (List / Status / History / …).
6. (если ставил голос) Отправь PTT — бот транскрибирует и пересылает
   текст в активную сессию.
7. (если ставил медиа) Отправь фото — оно ложится в
   `<workdir>/.ccbot-inbox/<timestamp>-<file>` и claude получает
   синтетическое сообщение.

Если что-то не работает — **сначала** глянь
`journalctl -u ccbot@$USER -f`, **потом** заглядывай в код.

---

## 12. Чеклист «всё ли сделано»

- [ ] `tmux`, `ffmpeg`, `git`, `curl`, `python3.12`, `uv` установлены.
- [ ] `claude --version` работает, `claude auth status` показывает
      активную подписку.
- [ ] `/opt/ccbot` склонирован (ветка `main`), `uv sync` отработал
      без ошибок.
- [ ] `uv run ruff check` и `uv run pyright src/ccbot/` чистые.
- [ ] `~/.ccbot/.env` существует, mode 600, заполнены
      `TELEGRAM_BOT_TOKEN` и `ALLOWED_USERS`.
- [ ] `ccbot hook --install` — в `~/.claude/settings.json` есть
      SessionStart hook.
- [ ] (опц.) `whisper-cli` в PATH, модель в `~/.ccbot/models/`.
- [ ] `ccbot@$USER.service` enabled и active.
- [ ] Telegram-DM реагирует, создаётся первая сессия, `/list`
      её видит.

Если хотя бы одна галочка не стоит — не отчитывайся «готово».

---

## 12.1. Local terminal (опционально)

В Settings → *Локальный терминал* (или `local_terminal`) трёхпозиционный
переключатель — `выкл` / `по кнопке` / `всегда`:

- `выкл` — бот ничего не открывает, кнопки нет.
- `по кнопке` — авто-спавна нет; в footer-строке рядом со
  *Стоп / Очистить / Меню* появляется *🖥 Терминал* у активной
  сессии, у которой нет аттаченного клиента к её tmux-окну.
- `всегда` — бот открывает терминал при создании каждой сессии
  и плюс показывает ту же кнопку, когда терминала нет.

На Linux нужен один из эмуляторов на PATH; бот автодетектит
gnome-terminal / konsole / kitty / wezterm / alacritty / tilix /
foot / xterm. Если ни один не нашёлся, тапни *🪄 Configure via
Claude* в том же экране настроек — будет подсказка как написать
свой шаблон в `local_terminal_cmd` (или env-переменную
`CCBOT_LOCAL_TERMINAL_CMD`).

Под капотом каждый локальный терминал подключается к собственной
*grouped session* `ccbot-w<wid>` (через `tmux new-session -t ccbot
-s ccbot-w<wid>`). Это нужно чтобы у каждого клиента был свой
current-window, иначе открытие терминала под новую сессию утаскивает
все остальные клиенты на это же окно.

---

## 13. Частые проблемы

| Симптом | Вероятная причина | Что делать |
|---------|-------------------|------------|
| `ModuleNotFoundError` при `uv run ccbot` | `uv sync` не выполнялся в `/opt/ccbot` | `cd /opt/ccbot && uv sync` |
| Бот молчит на сообщения | Юзер не в `ALLOWED_USERS` либо токен не тот | проверь `~/.ccbot/.env` и логи |
| `tmux: command not found` под systemd | `PATH` в юните не содержит `/usr/bin` | оставь `ExecStart=/usr/bin/env uv run python -m ccbot` как в шаблоне |
| Хук не срабатывает | `ccbot` не в PATH у claude-процесса | `which ccbot` под нужным юзером; либо подставь абсолютный путь в `~/.claude/settings.json` |
| Голос не распознаётся | `whisper-cli` не в PATH или модель битая | прогон `whisper-cli -m <path> -f sample.wav` руками |
| 401/403 от Telegram API | Токен отозван или сеть блокирует api.telegram.org | новый токен через BotFather или `TG_PROXY_URL` |
| *🖥 Терминал* не появляется | Настройка `local_terminal=off` или эмулятор не на PATH | в Settings выбери `по кнопке`/`всегда` и установи один из gnome-terminal / kitty / wezterm / alacritty |
| Кнопка *🖥 Терминал* пропадает после переключения сессии | у новой активной сессии уже есть аттаченный клиент к её окну | это by-design — кнопка показывается только когда клиента нет |

---

## 14. Что НЕЛЬЗЯ делать

- ❌ Запускать `claude` без `--dangerously-skip-permissions` — режим
  bypass-only зашит в архитектуру (см. `doc/dm-multisession-spec.md`).
- ❌ Класть токен в любой файл, попадающий под git.
- ❌ Использовать `git pull --rebase` или менять ветку, не уведомив
  пользователя.
- ❌ Менять `scripts/ccbot.service` локально на сервере и забывать
  синхронизировать с репо — всегда правь файл в репо и пересоздавай
  symlink в `/etc/systemd/system/`.
- ❌ Запускать второй экземпляр `ccbot` из обычного терминала, пока
  работает systemd-юнит — Telegram getUpdates конфликтует.
