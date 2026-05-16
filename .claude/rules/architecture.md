# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Telegram Bot (bot/ package)                   │
│  - DM-based routing: 1 user = active_session -> tmux window        │
│  - Inline ≡ Menu surface (List / Status / Shot / New / Archive /   │
│    Settings) hosting most actions; History is reached via switcher │
│    tap / Menu → List / /screenshot Back (pagination is the         │
│    affordance, no explicit History button)                         │
│  - Slash commands (bot/commands/):  lifecycle.py + info.py         │
│  - Callback dispatch (bot/callbacks/): one file per CB_* prefix    │
│  - Send text → Claude Code via tmux keystrokes                     │
│  - Forward /commands to Claude Code                                │
│  - Tool use → tool result: edit live card in-place                 │
│  - Interactive UI: AskUserQuestion / ExitPlanMode / Permission     │
│  - Per-user message queue + worker (merge, rate limit)             │
│  - MarkdownV2 output with auto fallback to plain text              │
│  - i18n via ccbot.i18n.t (en / ru / zh)                            │
├──────────────────────┬──────────────────────────────────────────────┤
│  markdown_v2.py      │  telegram_sender.py                         │
│  MD → MarkdownV2     │  split_message (4096 limit)                 │
│  + expandable quotes │                                             │
├──────────────────────┴──────────────────────────────────────────────┤
│  terminal_parser.py                                                 │
│  - Detect interactive UIs (AskUserQuestion, ExitPlanMode, etc.)    │
│  - Parse status line (spinner + working text)                      │
└──────────┬──────────────────────────────────────────────────────────┘
           │                              │
           │ Notify (NewMessage callback) │ Send (tmux keys)
           │                              │
┌──────────┴──────────────┐    ┌──────────┴──────────────────────┐
│  SessionMonitor         │    │  TmuxManager (tmux_manager.py)  │
│  (session_monitor.py)   │    │  - list/find/create/kill windows│
│  - Poll JSONL every 2s  │    │  - send_keys to pane            │
│  - Detect mtime changes │    │  - capture_pane for screenshot  │
│  - Parse new lines      │    └──────────────┬─────────────────┘
│  - Track pending tools  │                   │
│    across poll cycles   │                   │
└──────────┬──────────────┘                   │
           │                                  │
           ▼                                  ▼
┌────────────────────────┐         ┌─────────────────────────┐
│  TranscriptParser      │         │  Tmux Windows           │
│  (transcript_parser.py)│         │  - Claude Code process  │
│  - Parse JSONL entries │         │  - One window per       │
│  - Pair tool_use ↔     │         │    topic/session        │
│    tool_result         │         └────────────┬────────────┘
│  - Format expandable   │                      │
│    quotes for thinking │              SessionStart hook
│  - Extract history     │                      │
└────────────────────────┘                      ▼
                                    ┌────────────────────────┐
┌────────────────────────┐         │  Hook (hook.py)        │
│  SessionManager        │◄────────│  - Receive hook stdin  │
│  (session.py)          │  reads  │  - Write session_map   │
│  - Window ↔ Session    │  map    │    .json               │
│    resolution          │         └────────────────────────┘
│  - active_sessions     │
│    (user_id -> sid)    │         ┌────────────────────────┐
│  - Message history     │────────►│  Claude Sessions       │
│    retrieval           │  reads  │  ~/.claude/projects/   │
└────────────────────────┘  JSONL  │  - sessions-index      │
                                   │  - *.jsonl files       │
┌────────────────────────┐         └────────────────────────┘
│  MonitorState          │
│  (monitor_state.py)    │
│  - Track byte offset   │
│  - Prevent duplicates  │
│    after restart       │
└────────────────────────┘

Additional modules:
  screenshot.py       ─ Terminal text → PNG rendering (ANSI color, font fallback)
  transcribe.py       ─ Voice-to-text transcription via whisper.cpp / Apple / OpenAI
  i18n.py             ─ Per-user UI strings (en / ru / zh)
  naming.py           ─ Haiku-generated session names + readable summaries
  usage.py            ─ Token usage aggregator + per-session token alerts
  main.py             ─ CLI entry point
  utils.py            ─ Shared utilities (ccbot_dir, atomic_write_json)
  session_models.py   ─ Session / WindowState / ClaudeSession dataclasses
  session_recovery.py ─ Startup hygiene: reconcile w/ tmux + resolve stale window IDs
  session_claude_io.py─ Read-only Claude transcript discovery (encode_cwd, list, get)
  transcript_format.py─ Tool-summary + tool-result formatting (was inside TranscriptParser)

bot/ package (was bot.py before A1, split per CLAUDE.md size budget):
  __init__.py         ─ Re-exports create_bot, forward_command_handler
  app.py              ─ create_bot, post_init/shutdown, handler registration
  _common.py          ─ is_user_allowed, active_window, resolve_ident,
                       render_session_preview, set_view, open_more_in_place,
                       is_window_busy, shorten_workdir, CC_COMMANDS
  _usage_window.py    ─ Dedicated ccbot-usage tmux window for /usage queries
                        (captures pane with -S -100 scrollback so the
                        Current session / week rows survive the longer
                        modal body; parser picks the LAST modal header
                        in the buffer to ignore stale prior attempts)
  _session_create.py  ─ create_and_activate_session (dir-browser → tmux flow)
  messages.py         ─ text/voice/photo/document handlers, forward_command_handler,
                       bash !cmd capture
  session_events.py   ─ handle_new_message — claude → TG dispatch
  commands/lifecycle.py    ─ /new /list /use /rename /kill /done /stop
                            /menu /archive  (+ archive_session shared helper)
  commands/info.py         ─ /history /screenshot /usage  (+ emit_*)
  callbacks/__init__.py    ─ Top-level dispatcher; tries each handler in order
  callbacks/dir_browser.py ─ CB_DIR_*, CB_SESSION_*  (+ Haiku summary cache)
  callbacks/window_picker.py ─ CB_WIN_*
  callbacks/switcher.py    ─ CB_SW_*
  callbacks/archive.py     ─ CB_ARC_*
  callbacks/footer.py      ─ CB_FT_STOP/KILL/CLEAR/MORE
  callbacks/more_menu.py   ─ CB_MM_LIST/STATUS/SHOT/NEW/ARCHIVE/SETTINGS/BACK
  callbacks/settings.py    ─ CB_ST_GRP + CB_ST_PREV/LAG/VOICE/LANG/WDAY/APPROVE
  callbacks/confirm.py     ─ CB_CONF_KILL/DONE/DEL × YES/NO
  callbacks/history_pagination.py ─ CB_HISTORY_PREV/NEXT
  callbacks/interactive_ui.py     ─ CB_ASK_*  (Up/Down/Left/Right/Esc/Enter/...)
  callbacks/screenshot_keys.py    ─ CB_SCREENSHOT_REFRESH + CB_KEYS_*

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + send_with_fallback
  message_queue.py    ─ Per-user queue + worker (merge, status dedup)
  status_polling.py   ─ Background status line polling (1s interval) +
                       auto-approve hook for interactive prompts +
                       bg-window interactive-UI detection (suppress + stash)
  notifications.py    ─ Live card per session + push events + completion +
                       bg-status panel injection + active-quota glyph in header +
                       refresh_panel + repost_card (always-repost behaviour:
                       every user-msg replaces the card by a fresh one below)
  bg_status.py        ─ Per-user bg session status map (working/finished/error/
                       needs_action), context_pct, pending_interactive_ui;
                       render_panel for the active card's tail block (each row:
                       ``<emoji> <name> <status> · context N%``).
                       Persisted in state.json (status/last_change/context_pct;
                       pending UI re-detected after restart by terminal_parser).
  archive.py          ─ /archive page rendering + restore + idle/purge sweeps
  history.py          ─ Paginated /history rendering (with optional extra rows)
  quota_alerts.py     ─ Background /usage modal poll (default 10 min) →
                       5h/weekly band crossings 50/75/90 %
  inbox.py            ─ photo/document inbox under <workdir>/.ccbot-inbox/
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode / Permission UI +
                       adopt_interactive_msg / render_interactive_keyboard
                       (used by switcher tap to claim the carrier as the
                       interactive UI for a bg session whose prompt was stashed)
  directory_browser.py─ Directory + session picker UI builders
  switcher.py         ─ Inline session-switcher keyboard
  menu.py             ─ Footer / More / Settings keyboard composition;
                       [+ new] [≡ Menu] share the bottom row on screen="main"
  cleanup.py          ─ Per-window state cleanup on archive
  callback_data.py    ─ Callback data prefix constants
  tg_format.py        ─ Table/code overflow → file attachment

State files (~/.ccbot/ or $CCBOT_DIR/):
  state.json         ─ window states + display names + read offsets + user
                      settings (previews / live_lag / voice / card_history /
                      card_page_lines / card_inline_screenshots /
                      bg_notify_finished / bg_notify_error /
                      bg_notify_needs_action / language / weekly_reset_day /
                      auto_approve / local_terminal*) + bg_status snapshot
  session_map.json   ─ hook-generated window_id→session mapping
                       (SessionStart + UserPromptSubmit — the latter
                       self-heals stale entries on every prompt)
  monitor_state.json ─ poll progress (byte offset) per JSONL file
  ccbot.lock         ─ singleton flock held by main.py for the
                       process lifetime; a second start refuses with
                       sys.exit(1) to avoid Telegram getUpdates
                       cross-fire
```

## Key Design Decisions

- **DM-centric, not topic-centric** — single 1-1 chat per user; routing key is `active_sessions[user_id] -> session_id -> window_id`. Multiple parallel sessions per user, switcher in the most recent bot message.
- **Window ID-centric** — All internal state keyed by tmux window ID (e.g. `@0`, `@12`), not window names. Window IDs are guaranteed unique within a tmux server session. Window names are kept as display names via `window_display_names` map. Same directory can have multiple windows.
- **Hook-based session tracking** — Claude Code `SessionStart` + `UserPromptSubmit` hooks write `session_map.json`; monitor reads it each poll cycle. SessionStart catches new claude processes; UserPromptSubmit fires per prompt and rewrites the mapping if the existing entry diverges from the current `session_id` (self-heals after `/resume`, `/clear`, or bot-restart races that miss the SessionStart firing). The hook produces zero stdout and always exits 0 — required for safety because UserPromptSubmit would otherwise prepend stdout to the prompt or block on non-zero exits. Fast-path skips the atomic rewrite when nothing changed.
- **Tool use ↔ tool result pairing** — `tool_use_id` tracked across poll cycles; tool result edits the original tool_use Telegram message in-place.
- **MarkdownV2 with fallback** — All messages go through `safe_reply`/`safe_edit`/`safe_send` which convert via `telegramify-markdown` and fall back to plain text on parse failure.
- **No truncation at parse layer** — Full content preserved; splitting at send layer respects Telegram's 4096 char limit with expandable quote atomicity.
- Only sessions registered in `session_map.json` (via hook) are monitored.
- Notifications delivered to users via active_sessions reverse-map (claude session_id -> user with matching active session). Background sessions render their own per-session live cards.
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. Old state.json files keyed by window name are auto-migrated.
- **Singleton lock** — `main.py` acquires an exclusive `fcntl.flock(LOCK_EX | LOCK_NB)` on `$CCBOT_DIR/ccbot.lock` before any tmux / bot startup. `FD_CLOEXEC` prevents the lock from leaking into subprocess children. A contending instance hits `OSError`, logs the path, and exits with code 1 — the supervisor's restart-backoff then just waits for the existing instance to die.
- **Orphan-process hygiene** — `archive_session` and `idle_archive_sweep` follow `tmux kill_window` with `tmux_manager.kill_orphan_claude_processes(claude_session_id)`: pgrep + SIGTERM any `claude --resume <id>` survivors. Catches the rare case where `claude` traps SIGHUP or the bot crashed mid-archive, leaving an orphan writer on the session's JSONL. Self/parent PID guarded.
- **Orphan-window detection** — At startup, `session_recovery.detect_orphan_windows` lists tmux windows not bound to any Session record (excluding the reserved utility windows `__main__` / `ccbot-usage`) and logs WARNING. Never auto-kills: surfaces the failure mode without destroying user state.
