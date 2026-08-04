# ccbot

[![test](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml)
[![secrets-scan](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml)

[中文文档](README_CN.md) · [Русская документация](README_RU.md)

A personal Telegram bot that bridges a private DM to multiple parallel
Claude Code or Codex CLI sessions running in tmux. One user, N sessions, one inline
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

This fork deviates from upstream `ccbot` in ways that are intentional
and not negotiable:

- **DM-only.** No supergroup, no forum topics, no thread routing. The
  only chat the bot ever sees is a private 1-1 DM with an allowlisted
  Telegram user id.
- **Personal, allowlist-gated.** `ALLOWED_USERS` normally holds a
  single numeric Telegram id. Several ids are supported as a *shared
  workspace* — the session pool is global and every claude event is
  fanned out to each allowed user's own DM (own live card, own
  switcher). It is not multi-tenant: everybody sees everything. Any
  message from a non-allowlisted sender is silently dropped (no reply,
  no callback toast) — the bot looks inert to outsiders.
- **Bypass-only.** Claude is launched with
  `--dangerously-skip-permissions`; Codex uses
  `--dangerously-bypass-approvals-and-sandbox`. There is no permission relay UI in
  Telegram — if you don't trust the model with full host access, run
  upstream instead. (The residual Yes/No prompts that bypass mode does
  *not* cover — e.g. WebFetch domain trust — surface as a keyboard, or
  can be auto-answered via `Settings → Auto-approve`.)
- **Multi-session, inline-switcher.** A single user can have many
  sessions in the same DM; an inline keyboard under the most recent
  bot message switches between them.
- **Rich messages first.** Output goes out as a Bot API 10.1 rich
  message (native markdown: GFM tables ≤ 20 columns, headings,
  `<details>`, footnotes, math), falling back to the MarkdownV2
  pipeline (`telegramify-markdown`) and then to plain text on any
  failure. One-line fenced shell commands become inline code so Telegram
  exposes Copy; multiline blocks remain fenced. Kill switch:
  `CCBOT_RICH_MESSAGES=off`. Upstream uses HTML.
- **Hook-based session tracking.** The selected agent's `SessionStart` +
  `UserPromptSubmit` hooks write `session_map.json`; the monitor polls
  it. No reliance on process-tree introspection or claude SDK.
- **Voice transcription is local-first.** `whisper.cpp` (default) or
  Apple Speech via PyObjC on macOS — no API key required to run.

The full design rationale lives in `doc/dm-multisession-spec.md`. The
implementation map is in `doc/dm-multisession-plan.md`.

## Prerequisites

- **tmux** in `PATH`
- **Claude Code** CLI (`claude`) or **Codex CLI** (`codex`); Codex can
  authenticate through Telegram on first launch
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
uv sync
cp .env.example ~/.ccbot/.env   # fill in TELEGRAM_BOT_TOKEN + ALLOWED_USERS
ccbot hook --install            # Claude; add --backend codex for Codex
ccbot                           # foreground; for prod use the systemd unit
```

A full step-by-step Linux install (written for an AI agent to follow)
lives in `doc/install-linux.md`.

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
| `CCBOT_AGENT_BACKEND`       | `claude`     | initial global backend for a new state file |
| `CLAUDE_COMMAND`            | `claude`     | binary used to start a session |
| `CLAUDE_FLAGS`              | `--dangerously-skip-permissions` | flags appended to `claude` |
| `CODEX_COMMAND`             | `codex`      | Codex CLI binary (an absolute Termux path is accepted) |
| `CODEX_FLAGS`               | bypass + hook trust + hooks + `--no-alt-screen` | flags appended to `codex` |
| `CODEX_NAMING_MODEL`        | `gpt-5.6-luna` | lightweight Codex model for automatic session names |
| `SESSION_IDLE_TTL`          | `4h`         | active → archived after this much idleness |
| `ARCHIVE_PURGE_AFTER`       | `14d`        | archived sessions purged from state after this |
| `QUOTA_ALERT_POLL_INTERVAL` | `10m`        | how often the live `/usage` modal is sampled |
| `VOICE_BACKEND`             | `auto`       | `auto` / `whisper` / `apple` / `off` |
| `WHISPER_MODEL_PATH`        | `~/.ccbot/models/ggml-medium-q8_0.bin` | whisper.cpp model (falls back to a pre-existing `ggml-medium.bin`) |
| `WHISPER_LANG_MODEL_PATH`   | `~/.ccbot/models/ggml-tiny.bin` | tiny model for the language-detect pre-pass |
| `WHISPER_LANG_DEFAULT`      | `ru`         | language assumed when detection isn't confident |
| `WHISPER_THREADS`           | `6`          | threads for `whisper-cli` (its own default is 4) |
| `BG_STATUS_MAX`             | `4`          | max badges in the bg-status panel; older entries collapse to `+N more` |
| `CARD_EDIT_LAG`             | `2.0`        | coalescing window for live-card edits (seconds) |
| `CCBOT_RICH_MESSAGES`       | `on`         | `off` disables Bot API 10.1 rich messages (MarkdownV2 only) |
| `CCBOT_HOST`                | hostname     | deployment label exported to sessions as `CCBOT_HOST` |
| `TG_PROXY_URL`              | _(unset)_    | outbound proxy for the Bot API (`socks5://…` or `http://…`) |

The full list lives in `.env.example` and in
`doc/dm-multisession-spec.md` § 12. Per-user UI preferences (card
size, notifications, voice backend, language, …) are not env vars —
they live behind `≡ Menu → Settings`, see below. The agent is also a
persisted bot-wide setting at `Settings → Behavior → Agent`: one bot
instance runs either Claude or Codex. `CCBOT_AGENT_BACKEND` is only the
initial default when the state file has no saved selection.

## Hook setup

The bot tracks tmux-window-to-agent-session mappings via two lifecycle
hooks: `SessionStart` catches every new agent process, and
`UserPromptSubmit` self-heals a stale mapping on each prompt (covers
`/resume`, `/clear`, and bot-restart races). Auto-install once:

```bash
ccbot hook --install
```

For the Codex backend, install the equivalent hooks in
`~/.codex/hooks.json`, then select Codex in Telegram Settings:

```bash
ccbot hook --install --backend codex
```

Switching is blocked while the current backend still has live sessions;
archive or kill them first. Archived sessions retain their original backend.
Restoring an archive from the other backend performs a cross-agent pickup:
ccbot parses the source JSONL into a bounded handoff, starts a fresh native
target session, and records both the new target id and the source provenance.
The source transcript is never modified.

Codex-on-Termux setup is documented in
[`doc/install-termux.md`](doc/install-termux.md).

The installer is per-event idempotent — re-running it on an older
`SessionStart`-only install just adds the missing entry.

Or add manually to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ]
  }
}
```

## Usage

The bot exposes a small slash-command surface in the Telegram `/`-menu
plus an inline `≡ Menu` button on the most recent bot message:

| Command    | Effect |
| ---------- | ------ |
| `/menu`    | Open the inline ≡ Menu screen |
| `/help`    | Inline mini-doc with section buttons |
| `/history` | Full transcript of the active session (paginated) |
| `/done`    | Mark active session as done and archive it |

Claude Code's own pickers (`/model`, `/effort`, `/compact`, `/memory`)
are forwarded into the active session and published alongside them.
A few more commands work when typed but stay out of the `/`-menu:
`/new`, `/kill`, `/stop`, `/archive`, `/screenshot`, `/usage`,
`/health`, `/login`.

**`/login` — re-authenticating Claude from the phone.** When the OAuth
login behind `claude` expires, every session starts failing and there is
normally no way to fix it without a desktop. The bot needs no Claude
auth of its own, so it notices the failure, posts a 🔐 notice, and
`/login` runs the exchange for you: it hands you the OAuth link, you
approve it in the phone browser (the redirect goes to
`platform.claude.com`, not a localhost callback, so no tunnel is
needed), and you send the code the page shows back as an ordinary
message. The bot feeds it to the waiting process, confirms the new
deadline, and deletes your message with the code. It then reposts the
active session's card (or the Menu, if no session is active) so you carry
on where you were instead of scrolling back past the exchange.

With the Codex backend, authentication starts automatically on a fresh bot
launch. The bot checks `account/read` through Codex app-server; when no
account is present it sends the official device-login URL and user code to
Telegram, waits for `account/login/completed`, and then unlocks session
creation. The code is entered on the OpenAI page and is never sent back to
the chat. `/login` manually restarts the same flow.

The notice fires on Claude Code's own error entry (`isApiErrorMessage` /
`authentication_failed`), not on the error text — a session that merely
writes about a dead login won't trigger it. A fresh login moves the
credential deadline out by ~30 days; that deadline is the only thing that
matters, since refresh-token rotation keeps it fixed.

The remaining actions live behind the menu — `Sessions`, `Archive`,
`Status`, `New`, `Settings`. The 🧑‍💻 *Shot* (terminal screenshot)
button lives in the main view's control row and in *Menu → Sessions* —
next to *Kill* and *Clear* — so it's always reachable from the
transcript surface itself. Most users never type slash commands at all
once they discover the menu.

### Sessions and switcher

Send any text in the DM to start your first session — the bot opens a
directory browser, you pick the project, and a tmux window with
the selected agent starts there. Subsequent text in the DM is routed to the
**active** session.

Directory rows are ordered by the newest meaningful file change anywhere in
their nested contents. Generated dependency/cache trees are ignored, and the
scan runs off the Telegram event loop with a short cache.

Sessions are named after the directory basename and renamed once after
the first message of ≥ 20 chars by a small separate request: Haiku for
Claude or `CODEX_NAMING_MODEL` for Codex (default `gpt-5.6-luna`). The
result is a two-word intent summary such as `token budget`. Disable
automatic names in Settings to retain the directory name and skip the
extra model call.

The most recent bot message carries an inline session switcher
(`▷ session-A · session-B`) with a paired `[+ new] [≡ Menu]` row
anchored at the bottom — the two "go-elsewhere" affordances sit
side-by-side so the slot stays put across views (`[+ new] [Back]`
takes that spot in *Menu → Sessions* / *Archive*).

Switcher buttons read **oldest → newest**: each session keeps the same
slot for its whole life and a newly created one appends to the right,
so muscle memory survives switching. Restoring a session from the
archive re-enters it as the newest button rather than in its original
slot. The compact switcher under `/screenshot` uses the same order.

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
per row. Each row carries a short blurb made only from the user's first
messages, so it's obvious at a glance what a session was about. A
model-generated summary never replaces that text. Tap a session — the
carrier paints the actual
transcript read straight from the JSONL on disk; *Restore* / *Delete*
stay in the footer.

### Background sessions

Background (non-active) sessions have **no live card of their own** —
they never edit a card or surface an AskUserQuestion prompt in chat.
Their state surfaces as a compact panel at the bottom of the active
session's card:

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

On top of the badge, a background session can push a short one-liner
(`✅ [scraper] task complete`) on a *state transition*. Three
independent toggles under *Settings → 🔔 Notifications*, all on by
default: `Bg: task complete`, `Bg: errors`, `Bg: needs action`. Turn
them off to keep background work entirely silent.

### Live card

Each active session owns one live card message that the bot keeps
editing — header, paginated body, bg panel, footer keyboard. Every
message you send reposts the card below your text (there is one
canonical behaviour; the old `Card position` setting was retired).
Above the bg panel it prints the session's `context: N%` — Codex uses exact
`token_count` rollout data, while Claude uses an
approximation of Claude Code's `/context` computed from the JSONL,
typically within ±10 % of the modal.

Card knobs live under *Settings → 🃏 Card / view*:

| Setting | Default | Effect |
| ------- | ------- | ------ |
| `Card history` | `20` | end-of-turn boundaries seeded into a fresh card from the JSONL (survives bot restarts) |
| `Page size` | `20` lines | max lines per card page; longer bodies chunk across pages on paragraph/sentence boundaries |
| `Inline screenshots` | `off` | card becomes photo + caption — the photo is the live pane render (caption limit is 1024 chars, so shrink page size to compensate) |
| `Live lag` | `4s` | coalescing window for preview updates |

Telegram's chat-header **`typing…` indicator** is driven by real
claude events. As long as the active session keeps emitting (tool
calls, thinking, text), `typing…` stays on; an idle session lets it
fade within Telegram's ~5s window.

### Other settings

*≡ Menu → Settings* groups everything into five categories: 🃏 Card /
view, 🔔 Notifications, 🎙 Voice, 🖥 Local terminal, ⚙ Behavior &
language. Worth knowing:

- **Auto-approve** (`off` by default) — auto-answers the interactive
  Yes/No prompts that `--dangerously-skip-permissions` doesn't cover
  (WebFetch domain trust and friends). When an auto-Yes doesn't clear
  the prompt, the bot escalates to the manual keyboard instead of
  looping.
- **Local terminal** (`off` / `manual` / `auto`) — pops a native
  Terminal.app / iTerm2 / Linux emulator window attached to the
  session's tmux window, so you can drive the same session by hand.
  `manual` only shows the 🖥 *Term* button; `auto` also spawns one per
  new session. Killing the session closes the tab it opened.
- **Weekly reset** — only needed for Claude, whose `/usage` reports a
  clock time but not a complete timestamp. Codex supplies its exact
  reset timestamp through app-server.
- **Language** — `en` / `ru` / `zh` for the bot's own UI strings.

### Quota and status

*≡ Menu → 📊 Status* uses the selected backend's authoritative source:

- Claude: its live `/usage` modal through the dedicated `ccbot-usage`
  tmux window;
- Codex: `account/rateLimits/read` from the supported app-server API,
  without typing into a working session.

Claude retains the compact display:

```
Claude Code
🟡 5h: 62% · 12.4%/h · 17:00
🟢 week: 28% · 4.0%/d · Mon 17:00
🟢 week (Sonnet): 12% · Mon 17:00
```

For Codex, the weekly block focuses on the actionable calendar-day budget:

```
OpenAI Codex

🟢 5h

Used: 12%

Reset: 14:30

🟢 week

Used: 50%

Today: another 10.0%

Reset: 05.08 17:00
```

At the start of each local calendar day, the remaining weekly quota is
split equally across today and every remaining date through the reset
date, inclusive. The day's allocation stays fixed while `Today`
decreases by actual usage. Overspend is rendered as `over by N%`. On
the next date, the real remaining pool is redistributed, so both over-
and underspend adjust all following days. The daily baseline lives in
`$CCBOT_DIR/codex_quota_day.json` and survives bot restarts.

Claude's background poll runs every
`QUOTA_ALERT_POLL_INTERVAL` and pushes an alert when a 5h or weekly
band crosses 50 / 75 / 90 %. Only settled reads are published, so a
half-rendered modal can't fire a phantom alert.

### Voice and media

- **Voice messages** are transcribed locally (whisper.cpp / Apple
  Speech) and routed to the active session as if you typed them. The
  card shows a pending marker while the transcription runs, then the
  transcribed text in place, so you can verify what Claude received.
  On the arm64 reference host a voice message costs ~9s end to end:
  quantised `ggml-medium-q8_0` (1.8× faster than fp16, identical
  transcripts on the ru/en samples) plus a `ggml-tiny` language-detect
  pre-pass that lets the real run pin `-l` and encode once. Missing
  binary or model? *Settings → 🎙 Voice* offers a one-tap install
  (builds whisper.cpp, downloads both models).
- **Photos and documents** drop into `<workdir>/.ccbot-inbox/` and
  Claude is told via tmux. Files are auto-cleaned 24h after upload.
- **Outbound files** go the other way on demand: a session runs
  `ccbot send-file <path> [--caption TEXT]` and the bot delivers it
  into the DM right away (image extensions via `sendPhoto`, everything
  else via `sendDocument`). The command prints a pass/fail line per
  target chat, so Claude sees whether the delivery worked.
- **Forwarded posts with media** (channel posts with video / GIF /
  sticker that carry a caption) have the caption + any hidden
  `text_link` URLs extracted and routed to the active session,
  prefixed with `[forwarded from @channel]`. The media payload
  itself is dropped — Claude can't consume it.

## Architecture

The full module map is `.claude/rules/architecture.md`. At a glance:

```
src/ccbot/
├── main.py                 — CLI entry point (`ccbot`, `ccbot hook`, `ccbot send-file`)
├── config.py               — env-var loader (singleton)
├── session.py              — Session + SessionManager (state.json)
├── session_monitor.py      — JSONL polling, NewMessage callbacks
├── codex_session_io.py     — Codex rollout JSONL discovery and reading
├── codex_auth.py           — account/read + device-code login
├── codex_usage.py          — Codex app-server rate limits
├── session_import.py       — cross-agent restore handoff
├── transcript_parser.py    — JSONL turn parsing
├── terminal_parser.py      — interactive-UI + status-line detection
├── tmux_manager.py         — libtmux wrapper
├── rich.py                 — Bot API 10.1 rich messages (native markdown)
├── markdown_v2.py          — MD → Telegram MarkdownV2 (fallback path)
├── telegram_sender.py      — split_message at 4096-char limit
├── transcribe.py           — voice → text dispatcher
├── voice_install.py        — whisper.cpp + model auto-installer
├── send_file.py            — `ccbot send-file` outbound delivery
├── local_terminal.py       — native-terminal attach helper
├── usage.py                — token aggregator, context %, alert logic
├── i18n.py                 — en / ru / zh UI strings
├── bot/                    — Telegram-facing handlers (≤ 600 LOC each)
│   ├── app.py              — Application bootstrap, post_init / post_shutdown
│   ├── messages.py         — text / voice / photo / document / forward
│   ├── session_events.py   — claude → TG dispatch
│   ├── commands/           — slash command bodies
│   └── callbacks/          — one file per CB_* prefix
└── handlers/
    ├── notifications.py    — live cards + push events
    ├── card_model.py       — card state / render / paginate model layer
    ├── bg_status.py        — background-session status panel
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
| `session_map.json`  | hook-generated tmux-window → agent-session map |
| `monitor_state.json`| per-JSONL byte offsets (prevents duplicate notifications on restart) |
| `codex_quota_day.json` | Codex daily baseline and allocation |
| `ccbot.lock`        | exclusive flock held by the running bot; a second start refuses with exit 1 |

## Reliability

- **Single instance.** `main.py` holds an exclusive `flock` on
  `ccbot.lock` for its whole lifetime, so a supervisor restart racing
  a manual launch can't produce two bots fighting over `getUpdates`.
- **Long-poll watchdog.** A thread-based liveness check notices a
  silently hung long-poll, and a sustained network outage makes the
  process exit rather than sit there mute — the supervisor/systemd
  restarts it once the network is back.
- **Startup recovery.** Sessions whose tmux window survived are
  re-attached, vanished ones are marked `lost` (with a `Restore`
  button), and tmux windows bound to nothing are logged as orphans
  rather than killed.

## Deployment

A systemd unit is at `scripts/ccbot.service`; hosts with a flaky
uplink can instead run `scripts/ccbot-supervisor.sh`, which waits for
network before each start and restarts with backoff. For VPS hosts
that can't reach `api.telegram.org` directly, see `doc/deploy.md` for
the `TG_PROXY_URL` SSH-tunnel recipe.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: PRs that
align with the DM-only / personal-allowlist / bypass-only invariants
are welcome. CI must be green; pre-commit hooks must pass; one PR, one
purpose.

## Security

See [SECURITY.md](SECURITY.md) for the threat model and reporting
process. Vulnerabilities go through GitHub Security Advisories, not
public issues.

## License

See [LICENSE](LICENSE).
