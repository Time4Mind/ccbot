# DM Multi-Session Mode — Specification

Working spec for the fork. Replaces the topic-based supergroup model with a
single private DM where one bot manages multiple parallel Claude Code or
OpenAI Codex sessions.

Status: draft v0.1. Authoritative until superseded.

---

## 1. Goals and non-goals

### Goals

- Run the bot in a private 1-1 DM with a single user. No supergroup, no forum topics.
- Maintain N parallel sessions for one globally selected agent backend, each
  backed by an independent tmux window and native Claude or Codex process.
- Switching the active session does not stop or pause work in any other session.
- Preserve each agent's native feature surface: hooks, MCP, slash-commands,
  skills/plugins where supported, and voice/photo/document inputs.
- Authenticate through the CLI subscription: Claude OAuth or Codex
  app-server device login. No provider API key is required.
- Run on macOS (Apple Silicon) and Linux arm64.

### Non-goals

- Multi-user. The bot is personal; allowlist is a single Telegram user id.
- Webhooks / external event triggers (CI, GitHub, monitoring).
- Replacing the markdown→TG converter (we keep `telegramify-markdown` and tune around it).

---

## 2. Architecture overview

```
Telegram DM (1-1)
       |
       v
   ccbot (Python)
   - command router
   - active-session state
   - inline keyboards (A8)
   - session_monitor (JSONL polling)
   - notification dispatcher (C5+C7)
       |
       v
   tmux server (one per host)
   - one window per session
       |
       v
   claude --dangerously-skip-permissions
   or codex --dangerously-bypass-approvals-and-sandbox
   (one process per window)
   - reads/writes files
   - skills/plugins/hooks/MCP all native
```

State persistence in `$CCBOT_DIR`:

- `state.json` — active session per chat, session list with metadata
- `session_map.json` — session_id ↔ tmux window mapping
- `monitor_state.json` — JSONL read offsets per session
- `codex_quota_day.json` — Codex daily quota baseline and fixed allocation
- `ccbot.lock` — singleton flock held by `main.py` for the process lifetime; second-instance starts refuse with `sys.exit(1)`

Deployment target M3:

- Linux arm64 VPS as primary always-on host
- macOS as a thin client via `ssh -t <host> tmux attach -t ccbot`
- Bot, tmux server, and all claude processes live on the VPS

---

## 3. Session model (D4 — goal-driven)

A session is defined by its goal, not by its working directory. The user can `cd` inside a session freely — the session identity is preserved by name and by claude session id.

### Lifecycle

```
[create] -> active -> idle (4h no input) -> archived -> [restore | purged at 14d]
            ^                                   |
            +-----------------------------------+
                          restore
```

- **active**: tmux window alive, selected agent process running, in the inline switcher
- **idle**: tmux window alive, no input from user for >4h. Promoted to archived after the same threshold (kill window, persist state)
- **archived**: tmux window killed, backend and native session id stored.
  Same-backend restore uses `claude --resume <id>` or
  `codex resume <thread-id>`. Cross-backend restore creates a fresh target
  session from a bounded handoff. Visible in `/archive` for 0–72h, in
  `/archive --all` for up to 14d, then purged
- **purged**: state removed from `state.json`; transcripts on disk are kept for audit

### Goal closure (P1)

Only the user marks a goal as done via `/done <session>`. The bot never auto-closes a goal. `claude` can suggest closure in chat, but cannot execute it.

### Identification (H6)

- Each session has a stable short id (e.g. `a3f1`) and a human-readable name.
- The initial name is the workdir basename (`ccbot`, `ccbot-2`). It is
  replaced once on the first message by a one-shot naming call: Haiku for
  Claude or `CODEX_NAMING_MODEL` for Codex. A name that no longer matches
  the `basename` / `basename-N` pattern is treated as user-chosen.
- The result is capped at `_MAX_NAME_WORDS` (2), rejected on a refusal
  opener, and rendered with hyphens as spaces in the UI.
- The mechanism is opt-out through the session-naming setting
  (`haiku_naming`, retained as the persisted compatibility key). With it
  off, the basename sticks and no naming tokens are spent.
- There is no `/rename` command — the naming is automatic, and the switcher / archive views are the surfaces where names are read.

---

## 4. UX

### 4.1 Active session and switching (A4 + A8)

Exactly one session is "active" at any time. New free-text messages from the user go to the active session.

The inline switcher lives **only in the most recent bot message**. Previous bot messages have their inline keyboards stripped (`editMessageReplyMarkup` with empty markup) when a new bot message is sent. This keeps the chat from filling with stale buttons.

Switcher layout:

```
[v frontend] [scraper] [chores] [+ new]
```

- The active session is marked with a leading checkmark.
- Buttons are ordered oldest → newest by `created_at` (ties broken by session id), so a session holds a stable slot and a newly created one appends to the right. A session restored from the archive counts as newest (its `created_at` is bumped on restore). Every surface that renders session buttons — including the compact switcher under `/screenshot` — uses this same order.
- Tapping a non-active session triggers a callback. The bot edits the same message in place to show a context preview of the selected session and updates the active flag.
- After switching, all subsequent free-text from the user routes to the new active session. Background work in the previously active session continues.

Context preview format (calibratable):

```
<emoji> <name>  -  <state>  -  <token usage>
-----
You: <last user message, max PREVIEW_USER_LINES lines>
Claude: <last assistant message, max PREVIEW_ASSISTANT_LINES lines>
[Tool: <name> -> <first line of result>]   x PREVIEW_TOOLS
-----
[v scraper] [frontend] [chores] [+ new]
```

Defaults: `PREVIEW_USER_LINES=4`, `PREVIEW_ASSISTANT_LINES=8`, `PREVIEW_TOOLS=2`.

Live update of the preview:

- On click: snapshot at click time.
- After the click, if the previewed session emits new events on the bot side (assistant message, tool call), the preview message is `editMessageText`-updated.
- Coalesce updates with a base lag from the per-user `live_lag` setting (default 4 seconds). Setting `0` disables live updates.

### 4.2 Reply-quote (one-shot routing)

To send a single message to a non-active session without switching:

- Reply (Telegram native quote) to any bot message that belongs to that session.
- The text is routed to that session. Active session does not change.

This covers the "drop a quick note into a background session" case without breaking flow.

### 4.3 Slash commands (B7 — published via `setMyCommands`)

Only the truly user-needed commands are published in the Telegram `/`-menu;
everything else lives behind the inline ≡ Menu. Hidden commands still
work when typed.

Published:

| Command | Effect |
|---|---|
| `/menu` | Open the inline Menu surface (Sessions / Status / Shot / Archive / Settings). |
| `/help` | Inline mini-doc with section buttons. |
| `/history` | Paginated transcript of the active session from the JSONL. |
| `/done [name]` | Mark goal achieved. Archives with a "completed" tag. |

Forwarded Claude Code pickers (`/model` `/effort` `/compact` `/memory`) are also published when present in `CC_COMMANDS`.

Hidden (typed only):

| Command | Effect |
|---|---|
| `/new [name] [path]` | Create a new session. Without args, opens the directory browser. |
| `/kill [name]` | Stop tmux window and archive after confirmation. |
| `/stop` | Send Esc to the active session's tmux window (interrupt current task). |
| `/archive` | Show archived sessions, paginated, last 0–72h. |
| `/screenshot` | Snapshot the active session's tmux pane as a PNG. |
| `/usage` | Live account limits: Claude `/usage` modal or Codex app-server rate limits. |
| `/health` | Uptime, queue stats, latency, counters. |
| `/login` | Re-authenticate the selected backend. Claude takes its OAuth code back through chat; Codex uses the official device URL/code and waits for app-server completion without receiving the code in chat. |
| `/restore-file <msg_id>` | _(Planned — no handler implemented yet.)_ Re-fetch a previously-uploaded inbox file from Telegram. |

The legacy ``/status`` command was retired — Menu → Status surfaces
the same Anthropic-quota numbers via the dedicated `ccbot-usage` tmux
window.

`/stop` is also exposed as an inline button at the bottom of the active session's most recent bot message.

### 4.4 Notifications (C5 + C7)

Notifications come in two forms.

**Per-session card.** Each session has a single "live card" — a bot message that the bot keeps `editMessageText`-updating as new events arrive. The card shows:

- Session header: emoji + name + state + token usage
- Last tool/event line (single-line summary)
- Final result on completion (full)
- Last error if any

Edits do not trigger Telegram push notifications, so this is rate-limit friendly. The card is replaced (a new card sent, the old one finalized in chat history) on session completion or error.

**Push notifications** are sent as separate `send_message` calls only on key events:

- Task completion
- Error
- `AskUserQuestion` from claude

Format (C5: prefix + emoji):

```
<color-emoji> [<name>] <message>
```

Example: `🟦 [scraper] done in 4m. Wrote 3 files.`

Background sessions have no live card of their own. Their state shows
up as a per-row badge in the bg-panel at the bottom of the active
session's live card (see `handlers.bg_status.render_panel`), plus one
short push per *state transition* — `finished` / `error` /
`needs_action`. Each of the three is an independent user setting
(`bg_notify_finished` / `bg_notify_error` / `bg_notify_needs_action`,
default on); turning all three off makes background work silent
except for the badge.

### 4.5 Status (Menu → Status)

Menu → 📊 Status selects an authoritative source by backend. Claude
fetches its own `/usage` modal via the dedicated `ccbot-usage` tmux
window:

```
Claude Code
🟡 5h: 62% · 12.4%/h · 17:00
🟢 week: 28% · 4.0%/d · Mon 17:00
🟢 week (Sonnet): 12% · Mon 17:00
Extra: off
```

Codex calls `account/rateLimits/read` on its supported app-server API.
The 5h and weekly windows include their exact reset timestamps. The weekly
row also shows a fixed calendar-day budget:

```
OpenAI Codex

🟢 week

Used: 50%

Today: another 10.0%

Reset: 05.08 17:00
```

At the first reading on each local date, `(100 - used_percent)` is split
equally across all calendar dates through the reset date, inclusive.
Usage accumulated after that baseline reduces today's fixed allocation.
At the next date boundary the actual remaining pool is redistributed, so
over- and underspend affect every following day. State is persisted in
`codex_quota_day.json`; an initial mid-day observation can only establish
the baseline from that observation onward.

The locally-aggregated 5h/weekly token counter was retired — the
authoritative numbers come from Anthropic's own quota modal, so we
report exactly what it says (with our own burn-rate / reset suffix).

### 4.6 Per-session context fill (display only)

The live card and the bg-status panel show `context: N%` per session.
The number is computed from the session's JSONL transcript:

```
pct = round(
    (latest_assistant_turn.usage.input_tokens
     + latest_assistant_turn.usage.cache_creation_input_tokens
     + latest_assistant_turn.usage.cache_read_input_tokens)
    * 100
    / budget_for_model(latest_assistant_turn.model)
)
```

`budget_for_model` (in `usage.py`):

| Model | Budget |
|---|---|
| `claude-opus-4-7` / `claude-opus-4-6` / `claude-sonnet-4-6` | 1 000 000 |
| Everything else (Opus 4.5 / 4.1, Sonnet 4.5, Haiku 4.5, 3.x, …) | 200 000 |

Refresh fires in `session_events.handle_new_message` on every
assistant end-of-turn text turn and stashes the value on
`CardState.context_pct` / `BgStatus.context_pct`.

**Methodology note** — the value is an *approximation* of Claude
Code's own `/context` modal, typically within ±10 % relative. The two
diverge because `/context` additionally counts system prompt /
system tools / memory files / autocompact buffer that are not always
reflected in the last assistant turn's `cache_read`. We tried sending
`/context` into each pane periodically, but Claude Code writes the
modal output back into the JSONL as a fake user turn → pollutes the
live card AND eats real tokens from the session's context. JSONL
math is non-invasive and good enough for an at-a-glance signal.

---

## 5. Source of truth for usage

Transcript and quota data have separate sources:

- Claude transcripts:
  `~/.claude/projects/<project-hash>/<session-id>.jsonl`.
- Codex transcripts: rollout JSONL below
  `$CCBOT_CODEX_SESSIONS_PATH` (default `~/.codex/sessions`).
- Session monitoring reads each backend's native JSONL and normalizes turns
  without rewriting the source transcript.
- Account quota is never inferred from transcript tokens. Claude uses its
  live `/usage` modal; Codex uses app-server `account/rateLimits/read`.

---

## 6. Archive

### TTL

- 4h of no user input — session is archived.
- Archive action: kill tmux window, store `claude_session_id`, `workdir`, `name`, `created_at`, `archived_at`, `last_event` in `state.json`.

### Browsing

- `/archive` — paginated list, 0–72h, newest first, 5 per page.
- `/archive --all` — 0–14d.
- Each archived row has inline buttons:
  - `Restore` — recreate tmux window, run `claude --resume <id> --dangerously-skip-permissions` in the original workdir, move back to active. `created_at` is bumped to now, so the session re-enters the switcher as the newest button (§4.1).
  - `Delete` — purge state record (transcript files retained on disk).
  - `Inspect` — show last context (same format as A8 preview, no live update).

### Purge

- After 14d in archive, the state record is purged automatically. Transcripts on disk are kept for audit.

### Edge case: claude resume gotchas

- `claude --resume` is known to lose some MCP transient state and may have hook timeout issues (see ccbot upstream issue history).
- v0.1 accepts this. If empirically painful, raise idle TTL to 12h to reduce archive churn.

---

## 7. Files and media (I1 + I2 + TTL 24h)

### Inbound photos / documents

- Stored in `<workdir>/.ccbot-inbox/<utc-timestamp>-<filename>`.
- The active session receives a synthetic message in tmux: `"received file: .ccbot-inbox/<...>"`.
- Reply-quote routing applies: replying to a non-active session's message with a file attaches it to that session instead.
- TTL: files are deleted from the inbox 24h after upload. The bot retains the original Telegram `file_id` for 30d so `/restore-file <message-id>` can re-fetch from Telegram CDN.

### Outbound

- On-demand, not polled: a session hands the user a file by running `ccbot send-file <path> [--caption TEXT]` (`send_file.py`) directly — no drop directory, no delay. Image extensions go out via `send_photo`, everything else via `send_document`; the command prints a pass/fail line per target chat so the invoking tool call carries real feedback back to Claude.
- Target chat resolution: `--chat-id` override > `$CCBOT_CHAT_ID` (exported by `tmux_manager.create_window` at spawn time from the Telegram user who created/owns the session — see `owner_user_id`) > broadcast to every `ALLOWED_USERS` entry (used for windows with no single owner, e.g. the internal usage-check window).
- No MCP tool involved; Claude just needs to know the convention (documented for it via the container's `~/.claude/CLAUDE.md`, keyed off `CCBOT_INTERFACE=telegram` the same way output-format guidance is).

---

## 8. Voice (J4)

- Backend: `whisper.cpp` with `ggml-medium-q8_0.bin` (~785MB on disk).
- On macOS, optional Apple Speech Recognition backend via `python-speechrecognition`. Selected by `VOICE_BACKEND=auto|whisper|apple|off`. `auto` picks Apple on Darwin, whisper.cpp elsewhere.
- Subprocess call on each voice message. RAM is freed between calls; only `whisper.cpp` binary stays loaded (~10MB).
- Transcript is forwarded to the active session as if the user typed it. Reply-quote routing applies.
- No OpenAI API key required.

### Latency tuning (arm64)

A voice message used to cost ~32s end to end. Three changes took it to
~9s, all measured on the Kali-on-Android host (8 cores, `MATMUL_INT8` +
`i8mm`, whisper built with `REPACK=1`):

| Change | Effect |
|---|---|
| `ggml-medium-q8_0` instead of fp16 | 1.80–1.83× faster, **byte-identical** transcripts on the ru/en samples — the CPU has a native int8 path |
| Language-detect pre-pass on `ggml-tiny` | `-l auto` makes whisper run the **encoder twice** (12.4s of pure overhead on medium). Detecting on tiny costs 0.6s, then the real pass pins `-l` and encodes once |
| `-t 6` (was whisper-cli's own default of 4) | 14.2s → 12.1s at fp16; 2 cores left for the rest of the phone |

`_detect_language` only moves **off** `WHISPER_LANG_DEFAULT` on a
confident call (`p >= WHISPER_LANG_MIN_P`, default 0.9). Measured shape:
tiny detects English reliably (p ≥ 0.966 on every sample) but is shaky on
Russian (guessed `de` / `fr` / `da`, never above p=0.704) — so Russian
audio that tiny misreads still falls through to `ru`, and only a
high-confidence non-default wins. A missing tiny model is not an error;
the language just stays at the default.

Rejected after measuring: `whisper-server` (saves only the 0.93s model
load — the file is already in page cache), `large-v3-turbo` (its encoder
is ~2× medium's; turbo only accelerates the decoder, and the encoder is
our bottleneck), `-ac 256` (3.3× faster but visibly corrupts the text),
`-d` duration limits (the encoder always processes a full 30s window).

Install footprint:

- `whisper.cpp` binary: ~10MB
- Model `ggml-medium-q8_0.bin`: 785MB (one-time download)
- Model `ggml-tiny.bin`: 75MB (language detection, optional)
- Total: <1GB — comfortably under the original <2GB target.

---

## 9. Recovery (F2 + F3)

### Auto-recover on bot start (F2)

- Acquire `$CCBOT_DIR/ccbot.lock` (exclusive `fcntl.flock`). On contention, exit with code 1 — there is already a bot running and Telegram's `getUpdates` is exclusive per token. Lock is set with `FD_CLOEXEC` so it never leaks into subprocess children.
- Read `state.json`.
- For each session marked active or idle: check if its tmux window still exists.
  - If yes: re-attach. Re-bind monitor offsets.
  - If no: mark as `lost`. Surfaces in the switcher with a `Restore` button.
- For each archived session: nothing to do at startup.
- Walk live tmux windows. Windows not bound to any Session record (excluding the reserved utility windows `__main__` / `ccbot-usage`) are logged as `orphan_window` WARNINGs. Never auto-killed — surfaces the failure mode without destroying state. Typical cause: a window that survived `kill_window` during an earlier archive (claude trapped SIGHUP, or the bot crashed mid-archive).

### Archive cleanup

- `kill_window(window_id)` is followed by `tmux_manager.kill_orphan_claude_processes(claude_session_id)`: `pgrep` for any `claude --resume <id>` survivors and `SIGTERM` them. Self/parent PID guarded. Prevents two processes from later resuming the same session id and corrupting its JSONL.

### Manual restore (F3)

- Inline `Restore` button on archived and lost sessions.
- On restore: create tmux window, run `claude --resume <session-id> --dangerously-skip-permissions` in the original workdir, attach monitor, bump `created_at` so the session lands at the newest end of the switcher.

---

## 10. Auth (L1)

- `ALLOWED_USERS` env var: comma-separated Telegram numeric user ids. Single user expected.
- Any message from a non-allowed user is silently dropped, no reply.
- Bot token stored in `TELEGRAM_BOT_TOKEN` env var.
- No pairing flow. No 2FA. The single allowlist line is the entire auth model.

---

## 11. Deployment (M3)

### Primary: Linux arm64 VPS

- Systemd unit owns: tmux server, ccbot process, claude processes (children of tmux).
- Restart policy: `Restart=always`. On reboot, ccbot's auto-recover flow (section 9) handles state.
- whisper.cpp models are on local disk; `WHISPER_MODEL_PATH=/var/lib/ccbot/models/ggml-medium-q8_0.bin` (defaults to `$CCBOT_DIR/models/ggml-medium-q8_0.bin`, with an automatic fallback to a pre-existing `ggml-medium.bin`) and `WHISPER_LANG_MODEL_PATH` for the tiny language-detect model.

### Secondary: macOS as ssh client

- For "I want to see the terminal directly" moments: `ssh -t vps tmux attach -t ccbot`.
- Mac does not run its own ccbot instance. Single source of truth.
- The tmux session inside the VPS is the actual terminal; user can interact with claude there as well as via Telegram.

---

## 12. Telegram formatting (O1)

- Rendering pipeline: claude markdown → `telegramify-markdown` → Telegram HTML.
- Known weak spots and how we handle them:
  - **Tables**: if columns >3 OR rendered width >60 chars, send as `.md` file instead of inline.
  - **Long code blocks**: if block length >120 lines OR >3000 chars, send as `.<ext>` file. Inline preview keeps the first 30 lines.
  - **Nested lists**: keep as-is; calibrate based on real usage.
- Rendering rules live in `src/ccbot/tg_format.py` (new module). Tests cover known-bad inputs.

---

## 12. Configuration cheatsheet (env vars)

```
# Auth
TELEGRAM_BOT_TOKEN=<telegram bot token>
ALLOWED_USERS=<comma-separated tg user ids>

# Storage
CCBOT_DIR=/var/lib/ccbot

# Agent backend (initial default; the saved Settings choice wins later)
CCBOT_AGENT_BACKEND=claude       # claude | codex
CLAUDE_COMMAND=claude
CLAUDE_FLAGS=--dangerously-skip-permissions
CODEX_COMMAND=codex
CODEX_FLAGS=--dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust --enable hooks --no-alt-screen
CODEX_NAMING_MODEL=gpt-5.6-luna
# CCBOT_CODEX_SESSIONS_PATH=~/.codex/sessions

# Sessions
SESSION_IDLE_TTL=4h            # active -> archived
ARCHIVE_PURGE_AFTER=14d

# Quota alerts
QUOTA_ALERT_POLL_INTERVAL=10m  # background poll of /usage modal

# UI
PREVIEW_USER_LINES=4
PREVIEW_ASSISTANT_LINES=8
PREVIEW_TOOLS=2
CARD_EDIT_LAG=2.0              # live-card edit coalescing window (s)
BG_STATUS_MAX=4                # bg badges before collapsing to "+N more"
# Live-card coalescing also honours the per-user `live_lag` setting
# (default 4s). Everything else about the card (history depth, page size,
# inline screenshots, bg-notify toggles, language, auto-approve, local
# terminal, Haiku naming) is a per-user setting in state.json, not an env
# var — see SessionManager.DEFAULT_USER_SETTINGS.

# Rendering
CCBOT_RICH_MESSAGES=on         # off -> MarkdownV2 pipeline only

# Network / identity
TG_PROXY_URL=                  # socks5:// or http:// proxy for the Bot API
CCBOT_HOST=                    # deployment label exported into sessions

# Context-fill display (per-session %)
# Override if the host runs a non-Claude-Code model with a different
# context window; usage._budget_for_model handles the published list.

# Voice
VOICE_BACKEND=auto             # auto | whisper | apple | off
WHISPER_MODEL_PATH=/var/lib/ccbot/models/ggml-medium-q8_0.bin
WHISPER_LANG_MODEL_PATH=/var/lib/ccbot/models/ggml-tiny.bin
WHISPER_LANG_DEFAULT=ru        # assumed when detection isn't confident
WHISPER_LANG_MIN_P=0.9         # confidence needed to override the default
WHISPER_THREADS=6              # whisper-cli's own default is 4

# Claude
CLAUDE_COMMAND=claude
CLAUDE_FLAGS=--dangerously-skip-permissions
IS_SANDBOX=1                   # quiets non-interactive warnings
```

---

## 13. Acceptance criteria

The fork ships when all of the following are true on a fresh Linux arm64 VPS install:

1. Single `systemctl start ccbot` brings up the bot, tmux, and recovers any sessions from previous run.
2. In a private DM, sending text creates an auto-session, names it via Haiku, and routes the message to claude.
3. Inline switcher in the most recent bot message correctly toggles active session, edits the message in place with context preview, and stops further updates on stale messages.
4. Reply-quote on a non-active session's message routes a one-shot to that session without changing active.
5. Three concurrent sessions can run long-running tasks in parallel; switching between them does not pause any of them.
6. After 4h with no input, a session auto-archives. `/archive` shows it. `Restore` brings it back via `claude --resume`.
7. Voice message is transcribed locally via whisper.cpp; no OpenAI key configured.
8. Photo / document upload lands in `.ccbot-inbox` and the active session receives the relative path (optionally prefixed by the user's caption).
9. Menu → Status reflects Claude's live `/usage` modal (5h / weekly / Sonnet) — JSONL-derived counters were retired. Per-session ``context: N%`` is rendered on the card (JSONL approximation, ±10 % from `/context`).
10. `/done <name>` archives the session and `/archive` reflects it.
11. After VPS reboot, `systemctl restart ccbot` recovers all sessions whose tmux windows still exist; lost ones are listed with `Restore`.
12. No `--dangerously-load-development-channels`, no Anthropic API key, no OpenAI API key required for any of the above.

---

## 14. Open questions parked for later

- Table/code formatting heuristics — tune as edge cases appear.
- ``context: N%`` vs `/context` parity. Current JSONL math diverges
  from the modal by ±10 %; an exact match would need either a
  pollution-free way to invoke `/context` per session or a richer
  parse of the JSONL (system / tools / memory aren't always in
  `cache_read`). Acceptable as-is for the at-a-glance signal.
