# ccbot + Codex в Termux

Надёжная схема — держать `ccbot`, `tmux` и Codex в одном Debian/Ubuntu
`proot-distro`. Так у них совпадают `$HOME`, tmux socket, пути проектов и
`~/.codex/sessions`. Нативный Android/Termux использует Bionic, тогда как
готовый Codex CLI официально ориентирован на macOS/Linux; нативный запуск
поэтому остаётся best-effort.

## 1. Подготовить Termux

Установите Termux из F-Droid, разрешите работу в фоне и затем:

```bash
pkg update
pkg install proot-distro termux-services
proot-distro install debian
proot-distro login debian
```

Все следующие команды выполняются **внутри Debian**.

## 2. Установить зависимости

```bash
apt update
apt install -y git curl tmux procps util-linux ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Установите актуальный Codex CLI официальным standalone installer для Linux,
затем проверьте:

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
codex --version
codex login --device-auth
codex login status
```

## 3. Установить и настроить ccbot

```bash
git clone https://github.com/Time4Mind/ccbot.git
cd ccbot
uv sync
mkdir -p ~/.ccbot
cp .env.example ~/.ccbot/.env
```

Минимальные значения в `~/.ccbot/.env`:

```dotenv
TELEGRAM_BOT_TOKEN=token-from-botfather
ALLOWED_USERS=123456789
CCBOT_AGENT_BACKEND=codex
CODEX_COMMAND=codex
CODEX_FLAGS=--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust --enable hooks --no-alt-screen
VOICE_BACKEND=off
```

Установите Codex hooks и запустите бот:

```bash
uv run ccbot hook --install --backend codex
uv run ccbot
```

Первое сообщение в Telegram откроет выбор каталога. После выбора ccbot
создаст tmux-окно, Codex `SessionStart` hook свяжет его с thread id, а
следующие сообщения будут идти в ту же интерактивную сессию. Архив и
restore используют `codex resume <thread-id>`.

После первого запуска backend переключается глобально в
`Меню → Настройки → Агент, поведение и язык → Агент`. Значение сохраняется
в state бота; env задаёт только первоначальный default. Перед сменой агента
нужно завершить или архивировать все живые сессии текущего backend.

## 4. Фоновый запуск

Проект уже содержит supervisor без systemd. Запускайте его внутри того же
proot:

```bash
cd ~/ccbot
bash scripts/ccbot-supervisor.sh
```

Для старта после перезагрузки можно использовать Termux:Boot: его скрипт
должен войти в тот же `proot-distro` и запустить supervisor. Добавьте Termux
в исключения battery optimization и при необходимости включите
`termux-wake-lock`.

## Проверка

```bash
tmux list-windows -t ccbot
cat ~/.ccbot/session_map.json
find ~/.codex/sessions -name 'rollout-*.jsonl' | tail
```

В `session_map.json` у окна должны появиться `session_id` и
`backend: "codex"`. Не запускайте bot/tmux снаружи proot, а Codex внутри:
разные namespace, `$HOME` и tmux sockets не позволят ccbot управлять
процессом.
