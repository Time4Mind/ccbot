# ccbot deployment (DM multi-session)

Target: a Linux arm64 (or x86_64) host that runs always-on as the user's
single source of truth. macOS works as an interactive client via `ssh -t
<host> tmux attach -t ccbot`.

## Prerequisites

- Python 3.11+ (3.14 confirmed working)
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- `tmux` (≥3.0)
- `claude` CLI authenticated against `claude.ai` (Max x20 subscription) —
  `claude auth status` must succeed for the user that owns the bot
- `ffmpeg` if `VOICE_BACKEND=whisper`
- `whisper-cli` plus `ggml-medium.bin` if `VOICE_BACKEND=whisper`

## One-shot install

```bash
sudo install -d -m 755 /opt/ccbot
sudo chown $USER:$USER /opt/ccbot
git clone https://github.com/Time4Mind/ccbot.git /opt/ccbot
cd /opt/ccbot
uv sync --all-extras

# Provision the env file.
sudo install -d -m 750 /etc/ccbot
sudo install -m 640 .env.example /etc/ccbot/ccbot.env
sudo chown $USER:$USER /etc/ccbot/ccbot.env
$EDITOR /etc/ccbot/ccbot.env  # set TELEGRAM_BOT_TOKEN, ALLOWED_USERS, etc.

# Install the systemd template — replace USER with the bot's owner login.
sudo install -m 644 scripts/ccbot.service /etc/systemd/system/ccbot@.service
sudo systemctl daemon-reload
sudo systemctl enable --now ccbot@$USER.service
```

## Verification

```bash
systemctl status ccbot@$USER.service
journalctl -u ccbot@$USER.service -n 100 --no-pager
```

Open a DM with the bot in Telegram. Send any text. The bot should:

1. Announce that no active session exists and present the directory browser.
2. After you pick a directory, create a tmux window, register a Session,
   activate it, and forward your text to claude.
3. Subsequent assistant turns appear in chat with the inline session
   switcher under the latest content message.

## Recovery semantics

On `systemctl restart ccbot@…`:

1. `resolve_stale_ids()` re-binds persisted window IDs against live tmux
   windows (the tmux server itself survives the bot restart because we
   pin `TMUX_TMPDIR` to `/run/ccbot`).
2. `reconcile_sessions_with_tmux()` flips any Session whose window
   vanished into the `lost` state and clears the user's
   `active_sessions` pointer if it pointed at a lost record. Lost
   sessions are surfaced via `/archive --all` with a Restore button
   that runs `claude --resume <session-id>` in the original workdir.
3. Idle and archive sweeps resume from `last_event_at`/`archived_at`
   timestamps in `state.json`.

## Voice backend

- `VOICE_BACKEND=auto` (default) → Apple Speech on Darwin, whisper.cpp
  elsewhere. Apple currently delegates to whisper.cpp; adjust once a
  pure-Python AVSpeechRecognizer wrapper proves stable.
- `VOICE_BACKEND=whisper` → requires `WHISPER_BIN` (default
  `whisper-cli`) and `WHISPER_MODEL_PATH` (default
  `$CCBOT_DIR/models/ggml-medium.bin`, ~1.5GB).
- `VOICE_BACKEND=openai` → falls back to the legacy gpt-4o-transcribe
  HTTP path; needs `OPENAI_API_KEY`.
- `VOICE_BACKEND=off` → reject voice messages.

## State and disk usage

- `~/.ccbot/state.json` — sessions, active pointers, switcher trace.
- `~/.ccbot/session_map.json` — written by claude's `SessionStart` hook.
- `~/.ccbot/monitor_state.json` — JSONL byte offsets.
- `~/.ccbot/models/` — whisper model (only if VOICE_BACKEND=whisper).
- `<workdir>/.ccbot-inbox/` — uploaded photos/documents per session;
  pruned every hour past `INBOX_TTL_HOURS` (default 24h).
- Archived Session records expire after `ARCHIVE_PURGE_AFTER` (default
  14d). Transcripts on disk are kept for audit.

## Mac as a client

The bot does not run a second instance on the Mac. Instead:

```bash
ssh -t <linux-host> 'TMUX_TMPDIR=/run/ccbot tmux attach -t ccbot'
```

The Telegram side and the live tmux session share state, so anything you
type at the terminal is also seen by claude in the same session.
