# DM Multi-Session — Implementation Plan

Companion to `dm-multisession-spec.md`. Maps spec sections to concrete diffs, ordered for atomic commits.

Total refactoring scope: ~600–700 lines changed across 6 source modules and 4 test files. Surgical replacement of routing logic, not a rewrite.

---

## 0. Reference: code-surface map

### Files that change

| File | Lines now | Estimated touched | Why |
|---|---|---|---|
| `src/ccbot/session.py` | 893 | ~200 | Drop `thread_bindings`, add `active_sessions`. Replace 4 public methods. |
| `src/ccbot/bot.py` | 1909 | ~200 | Inbound routing pivot, drop topic lifecycle handlers, add slash commands, switcher injection. |
| `src/ccbot/handlers/message_queue.py` | 695 | ~30 | Re-key `_status_msg_info` and `_tool_msg_ids` from `(user, thread_id)` to `(user, window_id)`. Switcher injection. |
| `src/ccbot/handlers/cleanup.py` | 49 | ~20 | Simplify `clear_topic_state` → `clear_session_state` (no thread param). |
| `src/ccbot/handlers/status_polling.py` | 204 | ~50 | Drop topic-lifecycle loops, add idle-session detection for archive. |
| Tests (4 files) | ~600 | ~150 | Rewrite for new model. |

### Files left intact

- `tmux_manager.py`, `transcript_parser.py`, `terminal_parser.py`, `screenshot.py`, `hook.py`, `monitor_state.py`, `markdown_v2.py`, `telegram_sender.py` — all session-mechanism agnostic, no thread-id awareness.
- `session_map.json` semantics unchanged — keyed by tmux `window_id`, written by claude `SessionStart` hook.
- `AIORateLimiter` and per-user message queue keep their structure.

### Upstream `copilot/refactor-active-window-cc-session` — verdict

Parallel rewrite (76 files changed, 13.7k deleted, 2.9k added). Window-centric, not user-centric. Solves a different problem.

**Decision**: do NOT cherry-pick. Use as **reference only** for:

- `WindowState` dataclass shape (per-window persisted state)
- `/list`, `/use`, `/kill`, `/new` command surface naming

Our active-session-per-user model is incompatible with their per-window state machine.

---

## 1. Hotspot map (file:line)

### Inbound routing (TG → claude)
- `bot.py:164–171` — `_get_thread_id()` extractor (DELETE — DM has no thread_id).
- `bot.py:206, 225, 289, 314, 263, 417, 466, 587, 665, 880` — call sites of `resolve_window_for_thread()` and `get_window_for_thread()` (REPLACE with `get_active_session(user_id)`).
- `session.py:767–785` — `get_window_for_thread`, `resolve_window_for_thread` (DELETE).

### Outbound routing (claude event → TG)
- `bot.py:1710–1716` — `handle_new_message()`: iterates `find_users_for_session(session_id)` → `(user_id, window_id, thread_id)`. (REWRITE: build reverse map `session_id → active user`.)
- `session.py:797–810` — `find_users_for_session()` (REWRITE: walk `active_sessions` instead of `thread_bindings`).
- `session.py:787–795` — `iter_thread_bindings()` (DELETE; replace with `iter_active_sessions()`).

### Topic lifecycle (DELETE entirely — no topics in DM)
- `bot.py:258–270` — `on_topic_closed()`.
- `bot.py:413–438` — `on_topic_deleted()`.
- `bot.py:462–485` — `on_topic_edited()`.
- `session.py:750–761` — `unbind_thread()`.
- `handlers/cleanup.py` — `clear_topic_state()` simplified.

### Directory browser / session resume (KEEP, repoint)
- `handlers/directory_browser.py` — flow stays; final step changes from `bind_thread()` to `set_active_session()`.
- `bot.py:998–1107` — `_build_session_picker_and_browser()` — terminal call repointed.
- `bot.py:1440–1475` — session picker callback — terminal call repointed.

---

## 2. Atomic commit plan

Each step is meant to be a separately reviewable commit. Where size demands, split into 2–3 commits. Numbers are LOC estimates.

### Phase 0 — Foundation (no behavior change)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 0.1 | `chore(rules): replace topic-architecture.md with dm-architecture.md` | `.claude/rules/*.md`, `CLAUDE.md` | ~80 | Rules describe new model. Old rules archived under `doc/legacy/`. |
| 0.2 | `feat(state): add active_sessions and Session dataclass` | `session.py` | ~80 | Compiles, has fields, no callers yet. Migrations: state.json adds `active_sessions: {}` and `sessions: {}` keys, defaults safe. |
| 0.3 | `chore: extend env config for new vars` | `config.py`, `.env.example` | ~30 | All new env vars from spec section 14 read with defaults. No behavior change yet. |

### Phase 1 — Routing pivot

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 1.1 | `feat(routing): inbound — replace thread_bindings with active_sessions` | `bot.py`, `session.py` | ~120 | New helper `session_manager.get_active_session(user)`. All inbound handlers consult it. Topic-aware code paths gated behind `if thread_id is not None` for now (transitional). |
| 1.2 | `feat(routing): outbound — handle_new_message reverse-maps via active_sessions` | `bot.py`, `session.py` | ~80 | `find_users_for_session` rewritten, `iter_thread_bindings` removed. |
| 1.3 | `refactor: drop topic-lifecycle handlers` | `bot.py`, `handlers/cleanup.py`, `handlers/status_polling.py` | ~120 | `on_topic_closed/edited/deleted` deleted. Cleanup helper renamed and simplified. Bot does not register topic events anymore. |

After Phase 1 the bot routes correctly in DM but has no UI for switching — there's exactly one session (auto-created) at any time. We can integration-test up to here.

### Phase 2 — Switcher UI (A8)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 2.1 | `feat(switcher): inline session switcher under most recent bot message` | `handlers/message_queue.py`, `bot.py` (callbacks) | ~80 | After every content message, switcher row attached. New message strips inline keyboard from previous switcher. Unit test: 3 messages → only last has keyboard. |
| 2.2 | `feat(switcher): callback handler — switch active session` | `bot.py` | ~50 | Tapping a session button changes active session. Callback acks via `answer_callback_query`. |
| 2.3 | `feat(preview): edit message with context snapshot on switch` | `bot.py`, `transcript_parser.py` (read-only call) | ~120 | On switch, message is edited to show `name / state / usage / last user / last assistant / last 2 tools`. Sizes from env. |
| 2.4 | `feat(preview): live updates with PREVIEW_LIVE_LAG coalescing` | `handlers/message_queue.py` | ~70 | Subsequent events for the previewed session re-edit the preview message at most once per `PREVIEW_LIVE_LAG` seconds. Disabled when `0`. |

### Phase 3 — Slash commands (B7)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 3.1 | `feat(cmd): /new with optional name and path; auto-session on first message` | `bot.py`, `handlers/directory_browser.py` | ~100 | `/new`, `/new myname`, `/new myname ~/path`. First message in empty DM creates a default session. |
| 3.2 | `feat(cmd): /list /use /rename /kill /stop /done` | `bot.py`, `session.py` | ~150 | All commands functional. `/use <name>` mirrors switcher tap. `/done` archives with completed tag. `/kill` archives immediately. |
| 3.3 | `feat(cmd): publish setMyCommands on startup` | `bot.py`, `main.py` | ~30 | `/`-menu in TG shows the command list with descriptions. |

### Phase 4 — Sessions and archive

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 4.1 | `feat(session): goal field, life-cycle states (active/idle/archived)` | `session.py`, `state.json` schema | ~90 | New states wired through. Idle is purely informational v0.1. |
| 4.2 | `feat(archive): SESSION_IDLE_TTL — auto-archive idle sessions` | `handlers/status_polling.py`, `session.py` | ~80 | After 4h no input → tmux window killed, claude session id stored, session removed from switcher. Test with override `SESSION_IDLE_TTL=60s`. |
| 4.3 | `feat(archive): /archive with pagination, Restore/Inspect/Delete buttons` | `bot.py`, `handlers/` (new `archive.py`) | ~180 | List shows 5 per page, 0–72h. `--all` flag extends to 14d. Restore reruns `claude --resume <id>` in original cwd, makes session active. |
| 4.4 | `chore(archive): purge state record after 14d` | `handlers/status_polling.py` | ~30 | Daily sweep removes stale archive entries. Transcripts on disk kept. |

### Phase 5 — Naming and budgets

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 5.1 | `feat(autoname): Haiku-generated session name after first significant message` | `bot.py`, new `naming.py` | ~80 | Triggers on first user message ≥50 chars or after 2nd message. Single-shot Claude CLI call with `--model haiku --no-resume`. Failure → keep `session-N` placeholder. |
| 5.2 | `feat(usage): JSONL token aggregator (5h window + weekly)` | new `usage.py`, `session_monitor.py` | ~150 | Reads `usage.input_tokens` / `usage.output_tokens` from each assistant turn. Per-session and global counters maintained in memory + persisted hourly. |
| 5.3 | `feat(cmd): /status with usage breakdown` | `bot.py` | ~80 | Shows global 5h%, weekly%, per-session usage and state. Format from spec section 4.5. |
| 5.4 | `feat(quota): SESSION_TOKEN_BUDGET_5H soft warning at 75%` | `usage.py`, message dispatcher | ~40 | Warning posted to session card once per crossing. No hard block. |

### Phase 6 — Notifications (C5+C7)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 6.1 | `feat(notif): per-session live card with last tool/event` | `handlers/message_queue.py`, `bot.py` | ~120 | Card edited in place on each event. New card on completion or error. |
| 6.2 | `feat(notif): push messages on completion / error / AskUserQuestion` | `bot.py`, `handlers/message_queue.py` | ~60 | Format `<emoji> [name] message`. Color emoji per session, hashed from name. |
| 6.3 | `feat(notif): BG_NOTIFY_MODE switch — separate vs footer` | `handlers/message_queue.py` | ~50 | `separate` is default. `footer` appends background events to active session's last bot message with separator. Try `footer` in week 2. |

### Phase 7 — Media (J4 + I)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 7.1 | `feat(voice): whisper.cpp backend, on-demand subprocess` | new `voice/whisper_cpp.py`, replace `transcribe.py` | ~120 | Voice → ogg → `whisper-cli -m model.bin` → text → routed as user message. Apple Speech backend on Darwin if `VOICE_BACKEND=auto`. |
| 7.2 | `feat(voice): VOICE_BACKEND env switch + auto-detection` | `config.py`, `voice/__init__.py` | ~40 | `auto / whisper / apple / off`. Respect platform. |
| 7.3 | `feat(media): inbound photo/document → .ccbot-inbox + synthetic claude message` | `bot.py`, new `inbox.py` | ~100 | Files saved, claude told via tmux. TTL 24h, periodic cleanup. Original `file_id` retained 30d for `/restore-file`. |

### Phase 8 — Recovery and deployment

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 8.1 | `feat(recovery): startup re-attach + lost-session detection` | `main.py`, `session.py`, `handlers/status_polling.py` | ~100 | On boot, walks `active_sessions` and `archived`, validates tmux windows. Lost ones surface with `Restore` button in `/list`. |
| 8.2 | `chore(deploy): systemd unit + deployment notes` | `scripts/ccbot.service`, `doc/deploy.md` | ~80 | Single `systemctl enable --now ccbot` brings everything up. Restart=always. |

### Phase 9 — TG formatting (O1)

| # | Commit subject | Files | LOC | Acceptance |
|---|---|---|---|---|
| 9.1 | `feat(format): table/code overflow → file attachment` | new `tg_format.py`, `telegram_sender.py` | ~120 | Tables with >3 cols or width >60 → `.md` file. Code blocks >120 lines or >3000 chars → `.<ext>` file with 30-line inline preview. Tests with fixtures. |

### Phase 10 — Tests

| # | Commit subject | Files | LOC |
|---|---|---|---|
| 10.1 | `test: rewrite test_session.py for active_sessions` | tests | ~50 |
| 10.2 | `test: rewrite test_forward_command.py` | tests | ~40 |
| 10.3 | `test: rewrite test_interactive_ui.py keying` | tests | ~30 |
| 10.4 | `test: rewrite test_status_polling.py for archive flow` | tests | ~50 |
| 10.5 | `test: integration test — 3 parallel sessions, switching does not pause` | tests | ~80 |

---

## 3. Dependencies (commit order)

```
0.1, 0.2, 0.3
   |
   v
1.1 -> 1.2 -> 1.3
                |
                v
              2.1 -> 2.2 -> 2.3 -> 2.4
                                    |
                                    v
                                  3.1, 3.2, 3.3
                                    |
                                    v
                                  4.1 -> 4.2 -> 4.3 -> 4.4
                                                       |
                                                       v
                                                     5.1
                                                     5.2 -> 5.3 -> 5.4   (parallel after 4.x)
                                                       |
                                                       v
                                                     6.1, 6.2, 6.3       (parallel)
                                                     7.1, 7.2, 7.3       (parallel)
                                                     8.1, 8.2            (parallel)
                                                     9.1                 (anytime after 1.x)

Tests in phase 10 follow each refactor commit closely; do not batch all tests at the end.
```

Phase 1 is a hard prerequisite for everything. Phase 2 is hard prerequisite for 3.2 (`/use` reuses switch logic). 4.x is prerequisite for 5.x. The rest is parallel-safe within phases.

---

## 4. v0.1 done = acceptance from spec section 15

The plan is complete when all 12 acceptance criteria from `dm-multisession-spec.md §15` pass on a fresh Linux arm64 install.

---

## 5. Decisions parked for execution time

- **Calibration of `MAX_5H_TOKENS` / `MAX_WEEKLY_TOKENS`** — start with public estimates, tighten after week 1 of real usage.
- **`BG_NOTIFY_MODE=footer` empirical default** — try in week 2.
- **F5 hot-resume** — investigate after v0.1 ships and we have data on `claude --resume` reliability under bypass mode.
- **Table/code overflow thresholds** — calibrate per real failures (3 cols / 60 chars / 120 lines / 3000 chars are starting points).
