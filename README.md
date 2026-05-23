# ccbot

[![test](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml)
[![secrets-scan](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml)

[中文文档](README_CN.md) · [Русская документация](README_RU.md)

A personal Telegram bot that bridges a private DM to multiple parallel
Claude Code sessions running in tmux. One user, N sessions, one inline
switcher in the most recent bot message.

## Why

Claude Code lives in your terminal. Walk away from the desk and you
lose visibility — but the session keeps running. ccbot lets you:

- **Switch from desktop to phone mid-conversation.** Claude is doing a
  refactor; you go for a walk and keep monitoring + replying from
  Telegram.
- **Switch back to the desktop anytime.** Sessions live in real tmux
  windows, so `tmux attach` brings you straight back into the terminal
  with full scrollback.
- **Run several sessions in parallel.** Each session is its own tmux
  window with its own `claude` process. Switching the active session
  in Telegram doesn't pause any of the others.

The bot is a thin control layer over tmux — your Claude Code process
stays exactly where it is. ccbot just reads its output and sends
keystrokes.

## Differences from upstream

This fork (`feat/dm-multisession`) deviates from upstream `ccbot` in
ways that are intentional and not negotiable:

- **DM-only.** No supergroup, no forum topics, no thread routing. The
  only chat the bot ever sees is a private 1-1 DM with one allowlisted
  Telegram user id.
- **Single-user.** `ALLOWED_USERS` is expected to contain exactly one
  numeric Telegram id. Multi-tenant deployments are out of scope. Any
  message from a non-allowlisted sender is silently dropped (no reply,
  no callback toast) — the bot looks inert to outsiders.
- **Bypass-only.** `claude` is launched with
  `--dangerously-skip-permissions`. There is no permission relay UI in
  Telegram — if you don't trust the model with full host access, run
  upstream instead.
- **Multi-session, inline-switcher.** A single user can have many
  sessions in the same DM; an inline keyboard under the most recent
  bot message switches between them.
- **MarkdownV2** rendering pipeline (via `telegramify-markdown`) with
  automatic plain-text fallback on parse failure. Upstream uses HTML.
- **Hook-based session tracking.** A Claude Code `SessionStart` hook
  writes `session_map.json`; the monitor polls it. No reliance on
  process-tree introspection or claude SDK.
- **Voice transcription is local-first.** `whisper.cpp` (default) or
  Apple Speech via PyObjC on macOS — no API key required to run.

The full design rationale lives in `doc/dm-multisession-spec.md`. The
implementation map is in `doc/dm-multisession-plan.md`.

## Prerequisites

- **tmux** in `PATH`
- **Claude Code** CLI (`claude`) signed in with a Max subscription
- **Python 3.12+**
- **uv** (recommended) for dependency management
- macOS (Apple Silicon) or Linux arm64

Optional:

- **`ffmpeg`** + **`whisper-cli`** for local voice transcription
- **`pyobjc-framework-Speech`** for the native Apple Speech backend
  (`uv sync --extra apple-speech`)

## Quick start

```bash
git clone https://github.com/Time4Mind/ccbot.git
cd ccbot
git checkout feat/dm-multisession
uv sync
cp .env.example ~/.ccbot/.env   # fill in TELEGRAM_BOT_TOKEN + ALLOWED_USERS
ccbot hook --install            # one-time: register Claude Code SessionStart hook
ccbot                           # foreground; for prod use the systemd unit
```

## Configuration

Required env vars in `~/.ccbot/.env` (or `./.env`):

| Variable             | Description                                     |
| -------------------- | ----------------------------------------------- |
| `TELEGRAM_BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS`      | Single Telegram numeric user id                 |

Most-frequently-tweaked optionals:

| Variable                    | Default      | Effect |
| --------------------------- | ------------ | ------ |
| `CCBOT_DIR`                 | `~/.ccbot`   | Config and state directory |
| `TMUX_SESSION_NAME`         | `ccbot`      | tmux session that holds all session windows |
| `CLAUDE_COMMAND`            | `claude`     | binary used to start a session |
| `CLAUDE_FLAGS`              | `--dangerously-skip-permissions` | flags appended to `claude` |
| `SESSION_IDLE_TTL`          | `4h`         | active → archived after this much idleness |
| `ARCHIVE_PURGE_AFTER`       | `14d`        | archived sessions purged from state after this |
| `QUOTA_ALERT_POLL_INTERVAL` | `5m`         | how often the live `/usage` modal is sampled |
| `VOICE_BACKEND`             | `auto`       | `auto` / `whisper` / `apple` / `off` |
| `WHISPER_MODEL_PATH`        | `~/.ccbot/models/ggml-medium.bin` | whisper.cpp model |
| `BG_STATUS_MAX`             | `4`          | max badges in the bg-status panel; older entries collapse to `+N more` |
| `CARD_EDIT_LAG`             | `2.0`        | coalescing window for live-card edits (seconds) |
| `TG_PROXY_URL`              | _(unset)_    | outbound proxy for the Bot API (`socks5://…` or `http://…`) |

The full list lives in `doc/dm-multisession-spec.md` § 12.

## Hook setup

The bot tracks tmux-window-to-Claude-session mappings via Claude Code's
`SessionStart` hook. Auto-install once:

```bash
ccbot hook --install
```

Or add manually to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ]
  }
}
```

## Usage

The bot exposes a small slash-command surface in the Telegram `/`-menu
plus an inline `≡ Menu` button on the most recent bot message:

| Command  | Effect |
| -------- | ------ |
| `/menu`  | Open the inline ≡ Menu screen |
| `/done`  | Mark active session as done and archive it |

The remaining actions live behind the menu — `List`, `Status`,
`History`, `New`, `Archive`, `Settings`. The 🧑‍💻 *Shot* (terminal
screenshot) button lives in the main view's control row and in
*Menu → List* — next to *Kill* and *Clear* — so it's always reachable
from the transcript surface itself. Most users never type slash
commands at all once they discover the menu.

### Sessions and switcher

Send any text in the DM to start your first session — the bot opens a
directory browser, you pick the project, and a tmux window with
`claude` starts there. Subsequent text in the DM is routed to the
**active** session.

The most recent bot message carries an inline session switcher
(`▷ session-A · session-B`) with a paired `[+ new] [≡ Menu]` row
anchored at the bottom — the two "go-elsewhere" affordances sit
side-by-side so the slot stays put across views (`[+ new] [Back]`
takes that spot in *Menu → List* / *Archive*).

Tapping a non-active session **paints the full transcript history**
of that session onto the carrier message and switches the active
session in one go. Pagination buttons (◀ Older / Newer ▶) keep the
footer keyboard under them — they're the navigation affordance, so
there is no separate "History" entry in the Menu. Tapping the
already-active button is a no-op. `Back` from `/screenshot` reposts
the live card.

Reply-quoting a bot message belonging to a non-active session routes
that single reply there without changing the active session.

*Menu → Archive* shows a numbered list of past sessions, two buttons
per row. Each row carries a short blurb (Claude's own `type=summary`
entry, or the first user message) so it's obvious at a glance what a
session was about. Tap a session — the carrier paints the actual
transcript read straight from the JSONL on disk; *Restore* / *Delete*
stay in the footer.

### Background sessions

Background (non-active) sessions are **silent in chat** — they don't
emit live-card edits, push notifications, or AskUserQuestion prompts.
Their state surfaces only as a compact panel at the bottom of the
active session's card:

```
🟦 session-A ⏳        ← working in background
🟪 scraper   ✅        ← finished
🟧 chores    ❌        ← errored
🟨 frontend  ❓        ← needs user action (AskUserQuestion / permission)
```

The panel sticks across active-card edits so a finished bg session
isn't lost above a long tool log. Tap the badge's session in the
switcher to drop it from the panel (you've "seen" it). If the badge
shows `❓`, the switcher tap paints the stashed AskUserQuestion /
ExitPlanMode prompt with the same arrow/Enter/Esc keyboard you'd
get on a foreground prompt.

### Live card UX knobs

A fresh live card seeds itself with up to `CARD_SEED_TURNS` (default
20) recent end-of-turn boundaries from the session's JSONL transcript
so the history doesn't disappear across a bot restart.

`Settings → Card position` controls how your outgoing text relates
to the live card:
- `push` — leave it (your message scrolls the card up; default)
- `delete` — bot deletes your message so the card stays the latest
- `repost` — bot resends the card below your message and drops the
  old one

Telegram's chat-header **`typing…` indicator** is driven by real
claude events. As long as the active session keeps emitting (tool
calls, thinking, text), `typing…` stays on; an idle session lets it
fade within Telegram's ~5s window.

### Voice and media

- **Voice messages** are transcribed locally (whisper.cpp / Apple
  Speech) and routed to the active session as if you typed them.
  The reply echoes the transcribed text so you can verify what
  Claude received.
- **Photos and documents** drop into `<workdir>/.ccbot-inbox/` and
  Claude is told via tmux. Files are auto-cleaned 24h after upload.
- **Forwarded posts with media** (channel posts with video / GIF /
  sticker that carry a caption) have the caption + any hidden
  `text_link` URLs extracted and routed to the active session,
  prefixed with `[forwarded from @channel]`. The media payload
  itself is dropped — Claude can't consume it.

## Architecture

The full module map is `.claude/rules/architecture.md`. At a glance:

```
src/ccbot/
├── main.py                 — CLI entry point (`ccbot`, `ccbot hook`)
├── config.py               — env-var loader (singleton)
├── session.py              — Session + SessionManager (state.json)
├── session_monitor.py      — JSONL polling, NewMessage callbacks
├── transcript_parser.py    — JSONL turn parsing
├── terminal_parser.py      — interactive-UI + status-line detection
├── tmux_manager.py         — libtmux wrapper
├── markdown_v2.py          — MD → Telegram MarkdownV2
├── telegram_sender.py      — split_message at 4096-char limit
├── transcribe.py           — voice → text dispatcher
├── usage.py                — token aggregator + alert logic
├── i18n.py                 — en / ru / zh UI strings
├── bot/                    — Telegram-facing handlers (≤ 600 LOC each)
│   ├── app.py              — Application bootstrap, post_init / post_shutdown
│   ├── messages.py         — text / voice / photo / document / forward
│   ├── session_events.py   — claude → TG dispatch
│   ├── commands/           — slash command bodies
│   └── callbacks/          — one file per CB_* prefix
└── handlers/
    ├── notifications.py    — live cards + push events
    ├── archive.py          — /archive page rendering + idle sweeps
    ├── quota_alerts.py     — background /usage poll
    ├── interactive_ui.py   — AskUserQuestion / ExitPlanMode
    ├── menu.py             — inline-keyboard composition
    └── …
```

State is kept under `$CCBOT_DIR` (defaults to `~/.ccbot/`):

| File                | Contents |
| ------------------- | -------- |
| `state.json`        | sessions, active_sessions, window states, user settings |
| `session_map.json`  | hook-generated tmux-window → claude-session map |
| `monitor_state.json`| per-JSONL byte offsets (prevents duplicate notifications on restart) |

## Deployment

A systemd unit is at `scripts/ccbot.service`. For VPS hosts that can't
reach `api.telegram.org` directly, see `doc/deploy.md` for the
`TG_PROXY_URL` SSH-tunnel recipe.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: PRs that
align with the DM-only / single-user / bypass-only invariants are
welcome. CI must be green; pre-commit hooks must pass; one PR, one
purpose.

## Security

See [SECURITY.md](SECURITY.md) for the threat model and reporting
process. Vulnerabilities go through GitHub Security Advisories, not
public issues.

## License

See [LICENSE](LICENSE).
