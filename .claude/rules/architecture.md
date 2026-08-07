# System Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Telegram Bot (bot/ package)                   │
│  - DM-based routing: 1 user = active_session -> tmux window        │
│  - Inline ≡ Menu surface (Sessions / Status / New / Archive /      │
│    Settings) hosting most actions; History is reached via switcher │
│    tap / Menu → Sessions / /screenshot Back (pagination is the     │
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
  transcribe.py       ─ Voice-to-text transcription via whisper.cpp / Apple Speech
  i18n.py             ─ Compatibility facade for per-user translations
  i18n_locales/       ─ English / Russian / Chinese translation catalogs
  naming.py           ─ lightweight-model-generated session names
  usage.py            ─ Token usage aggregator + per-session token alerts
  main.py             ─ CLI entry point (ccbot / ccbot hook / ccbot send-file)
  config.py           ─ Env-var loader (singleton `config`), .env priority
  send_file.py        ─ `ccbot send-file` — on-demand outbound delivery
                       (photo vs document by extension; chat resolution:
                       --chat-id > $CCBOT_CHAT_ID > all ALLOWED_USERS)
  utils.py            ─ Shared utilities (ccbot_dir, atomic_write_json)
  session_models.py   ─ Session / WindowState / ClaudeSession dataclasses
  session_state.py    ─ active routing, lifecycle, settings, persistence helpers
  session_map.py      ─ hook map reconciliation and transcript resolution
  session_recovery.py ─ Startup hygiene: reconcile w/ tmux + resolve stale window IDs
  session_claude_io.py─ Read-only Claude transcript discovery (encode_cwd, list, get)
  transcript_format.py─ Tool-summary + tool-result formatting (was inside TranscriptParser)
  transcript_types.py ─ ParsedEntry / ParsedMessage / pending-tool DTOs
  transcript_message.py / transcript_codex.py ─ backend-specific normalization
  terminal_usage.py   ─ /usage models and terminal-output parsing
  tmux_process.py     ─ orphan process cleanup
  tmux_window.py      ─ backend command assembly and tmux window creation
  logging_setup.py    ─ Logging config (level via LOG_LEVEL, JSON via CCBOT_LOG_FORMAT)
  metrics.py          ─ In-process counters → metrics.json
  rich.py             ─ Bot API 10.1 rich messages via raw Bot._post
                       (sendRichMessage / rich edit; to_rich_markdown
                       escapes bare < and maps expandable-quote sentinels
                       to <details>); safe_* try rich first, fall back to
                       MarkdownV2 (kill switch CCBOT_RICH_MESSAGES=off)
  voice_install.py    ─ whisper.cpp auto-installer (binary + medium-q8_0
                       model + tiny language-detect model), driven from
                       Settings → Voice
  local_terminal.py   ─ Native-terminal attach (drives the local_terminal* settings)
  claude_auth.py      ─ Claude OAuth re-login driven from chat: auth-failure
                       detection (is_auth_failure_event — gated on the JSONL's
                       isApiErrorMessage flag via NewMessage.api_error, never on
                       error wording), credential-deadline read, and a pty-backed
                       `claude auth login` child whose URL goes to the chat and
                       whose code comes back from it (per-user flow + TTL)

bot/ package (was bot.py before A1, split per CLAUDE.md size budget):
  __init__.py         ─ Re-exports create_bot, forward_command_handler
  app.py              ─ Compatibility facade + watchdog/error handling
  _app_lifecycle.py   ─ post_init/post_shutdown orchestration
  _app_routes.py      ─ Application construction and handler registration
  _common.py          ─ is_user_allowed, active_window, resolve_ident,
                       render_session_preview, set_view, open_more_in_place,
                       is_window_busy, shorten_workdir, CC_COMMANDS
  _usage_window.py    ─ Dedicated ccbot-usage tmux window for /usage queries
                        (captures pane with -S -100 scrollback so the
                        Current session / week rows survive the longer
                        modal body; parser picks the LAST modal header
                        in the buffer to ignore stale prior attempts)
  _session_create.py  ─ create_and_activate_session (dir-browser → tmux flow)
  messages.py         ─ Compatibility facade for inbound message handlers
  _messages_shared.py ─ delivery proof, card bracketing, prompt interception
  _messages_text.py   ─ text routing and bash capture
  _messages_voice.py  ─ voice checkpoint/transcription routing
  _messages_media.py  ─ photo/document/forwarded content handling
  session_events.py   ─ handle_new_message — claude → TG dispatch
  commands/lifecycle.py    ─ /new /kill /done /stop /menu /archive
                            (+ archive_session shared helper)
  commands/info.py         ─ /history /screenshot /usage /health /help (+ emit_*)
  commands/auth.py         ─ /login re-auth flow (+ maybe_consume_code,
                            notify_auth_expired)
  callbacks/__init__.py    ─ Top-level dispatcher; tries each handler in order
  callbacks/dir_browser.py ─ CB_DIR_*, CB_SESSION_*
  callbacks/window_picker.py ─ CB_WIN_*
  callbacks/switcher.py    ─ CB_SW_*
  callbacks/archive.py     ─ CB_ARC_*
  callbacks/footer.py      ─ CB_FT_STOP/KILL/CLEAR/MORE
  callbacks/more_menu.py   ─ CB_MM_LIST/STATUS/SHOT/NEW/ARCHIVE/SETTINGS/BACK
  callbacks/settings.py    ─ CB_ST_GRP + CB_ST_LAG/VOICE/LANG/WDAY/APPROVE
  callbacks/confirm.py     ─ CB_CONF_KILL/DONE/DEL × YES/NO
  callbacks/history_pagination.py ─ CB_HISTORY_PREV/NEXT
  callbacks/interactive_ui.py     ─ CB_ASK_*  (Up/Down/Left/Right/Esc/Enter/...)
  callbacks/screenshot_keys.py    ─ CB_SCREENSHOT_REFRESH + CB_KEYS_*
  callbacks/help.py        ─ CB_HLP_HOME / CB_HLP_SEC (inline /help doc)
  callbacks/auth.py        ─ CB_AUTH_LOGIN / CB_AUTH_CANCEL (🔐 re-login)

Handler modules (handlers/):
  message_sender.py   ─ safe_reply/safe_edit/safe_send + send_with_fallback
  status_polling.py   ─ Background status line polling (1s interval) +
                       auto-approve hook for interactive prompts +
                       bg-window interactive-UI detection (suppress + stash)
  status_approval.py  ─ pure auto-approval parsing/signature helpers
  notifications.py    ─ Compatibility facade for live-card orchestration +
                       bg-status panel injection +
                       refresh_panel + repost_card (always-repost behaviour:
                       every user-msg replaces the card by a fresh one below)
  bg_status.py        ─ Per-user bg session status map (working/finished/error/
                       needs_action), context_pct, pending_interactive_ui;
                       render_panel for the active card's tail block (each row:
                       ``<emoji> <name> <status> · context N%``).
                       Persisted in state.json (status/last_change/context_pct;
                       pending UI re-detected after restart by terminal_parser).
  archive.py          ─ /archive page rendering + restore + idle/purge sweeps
  archive_blurb.py    ─ archive summary text cleanup and formatting
  history.py          ─ Live paginated /history cache and presentation
  history_archive.py  ─ archived transcript/card page rendering
  quota_alerts.py     ─ Background /usage modal poll (default 10 min) →
                       5h/weekly band crossings 50/75/90 %
  inbox.py            ─ photo/document inbox under <workdir>/.ccbot-inbox/
  interactive_ui.py   ─ AskUserQuestion / ExitPlanMode / Permission UI
                       (handle_interactive_ui + _build_interactive_keyboard).
                       A switcher tap surfaces a bg session's stashed prompt
                       via notifications.enter_kb_mode on the claimed carrier.
  directory_browser.py─ Directory + session picker UI builders
  switcher.py         ─ Inline session-switcher keyboard
  menu.py             ─ Footer / More keyboard composition and settings facade;
                       [+ new] [≡ Menu] share the bottom row on screen="main"
  cleanup.py          ─ Per-window state cleanup on archive
  callback_data.py    ─ Callback data prefix constants
  tg_format.py        ─ Table/code overflow → file attachment
  card_model.py       ─ Compatibility facade for card types/render/pagination
  card_types.py       ─ Event/CardState data only
  card_events.py      ─ monitor message → card event conversion
  card_text.py        ─ transcript text parsing and sanitization
  card_budget.py      ─ line/byte budgeting and chunking
  card_event_render.py─ individual event rendering
  card_pagination.py  ─ page boundaries and user-specific page sizing
  card_layout.py      ─ complete card body composition
  card_registry.py    ─ mutable card ownership, locks, and message registry
  card_seed.py        ─ JSONL seeding
  card_carrier.py     ─ pause/transfer/restore carrier lifecycle
  card_transport.py   ─ Telegram send/edit/photo operations
  card_updates.py     ─ event application/finalization/attachments
  card_stall.py       ─ stall detection and repost recovery
  card_surface.py     ─ timers, panel refresh, and receipt scheduling
  kb_mode.py          ─ kb-mode keyboard builder + pane-capture-to-PNG helper
  typing.py           ─ Per-user throttle in front of send_chat_action(TYPING)
                       (status_polling + session_events share one timer)

Responsibility-level extension guide: `doc/refactor-architecture.md`.

State files (~/.ccbot/ or $CCBOT_DIR/):
  state.json         ─ window states + display names + read offsets + user
                      settings (live_lag / voice / card_history /
                      card_page_lines / card_inline_screenshots /
                      bg_notify_finished / bg_notify_error /
                      bg_notify_needs_action / language / weekly_reset_day /
                      auto_approve / local_terminal* / haiku_naming)
                      + bg_status snapshot
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
- Notifications delivered to users via active_sessions reverse-map (claude session_id -> user with matching active session). Background sessions emit no chat messages of their own; their state surfaces only as a panel row at the bottom of the active session's live card (`handlers.bg_status.render_panel`).
- **Startup re-resolution** — Window IDs reset on tmux server restart. On startup, `resolve_stale_ids()` matches persisted display names against live windows to re-map IDs. Old state.json files keyed by window name are auto-migrated.
- **Singleton lock** — `main.py` acquires an exclusive `fcntl.flock(LOCK_EX | LOCK_NB)` on `$CCBOT_DIR/ccbot.lock` before any tmux / bot startup. `FD_CLOEXEC` prevents the lock from leaking into subprocess children. A contending instance hits `OSError`, logs the path, and exits with code 1 — the supervisor's restart-backoff then just waits for the existing instance to die.
- **Orphan-process hygiene** — `archive_session` and `idle_archive_sweep` follow `tmux kill_window` with `tmux_manager.kill_orphan_claude_processes(claude_session_id)`: pgrep + SIGTERM any `claude --resume <id>` survivors. Catches the rare case where `claude` traps SIGHUP or the bot crashed mid-archive, leaving an orphan writer on the session's JSONL. Self/parent PID guarded.
- **Orphan-window detection** — At startup, `session_recovery.detect_orphan_windows` lists tmux windows not bound to any Session record (excluding the reserved utility windows `__main__` / `ccbot-usage`) and logs WARNING. Never auto-kills: surfaces the failure mode without destroying user state.
