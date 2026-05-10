# Operational secrets — where things live

Quick reference so a fresh Claude session knows where to look (and
where **not** to put) credentials when working on or with ccbot.

## Where things actually are

| What | Where |
| ---- | ----- |
| ccbot Telegram bot token | `~/.ccbot/.env` → `TELEGRAM_BOT_TOKEN=…` (or `./.env` in cwd) |
| ccbot allowlist (Telegram user ids) | `~/.ccbot/.env` → `ALLOWED_USERS=…` |
| ccbot outbound TG proxy (optional) | `~/.ccbot/.env` → `TG_PROXY_URL=…` |
| OpenAI fallback voice key (optional) | `~/.ccbot/.env` → `OPENAI_API_KEY=…` |
| Claude Code login token | `claude auth status` — managed by the CLI, not a file in the repo |
| whisper.cpp model | `~/.ccbot/models/ggml-medium.bin` (path overridable via `WHISPER_MODEL_PATH`) |
| ccbot persisted state | `~/.ccbot/state.json` — non-secret, but contains user ids / paths |

`~/.ccbot/` itself is overridable via `CCBOT_DIR=…`. Local `./.env`
beats the global one when both are present (see `config.py`).

## Where things must NOT be

- **Not in `CLAUDE.md`** (project or global). CLAUDE.md is committed
  / synced; secrets in it leak the moment the repo goes public, gets
  forked, or syncs to another machine.
- **Not in any file under git tracking.** `.gitleaks.toml` and the
  pre-commit `forbid-personal-markers` hook will block it, but the
  cheap rule is "don't paste the token into anything tracked".
- **Not in `~/.claude/CLAUDE.md`.** Even though it's user-private,
  treating user-global instructions as a place for secrets builds the
  wrong habit and makes it easy to copy-paste them somewhere worse
  later.

## When asked "where's the bot token?"

1. Check `~/.ccbot/.env` first.
2. If missing, check the cwd for a local `.env`.
3. Don't grep `CLAUDE.md` files for tokens — that's a smell, not a
   storage location.
4. If you actually need the value at runtime, read the env var
   ``TELEGRAM_BOT_TOKEN`` directly from the running ccbot process —
   it's loaded into config at boot. Don't print it back to the chat.

## Generating a fresh token

If the user wants a new token: `@BotFather` → `/revoke` → save the
new value into `~/.ccbot/.env`, restart via `./scripts/restart.sh`.
Never commit the file.
