"""English source translation table for the Telegram UI."""

from __future__ import annotations

EN: dict[str, str] = {
    # Voice delivery
    "voice.not_delivered": (
        "🎙 Voice didn't reach the session — it has an open prompt "
        "(permission/question), so the transcribed text can't go in. "
        "Answer the prompt and resend the voice message."
    ),
    "voice.download_failed": (
        "🎙 Voice didn't reach the session: Telegram couldn't provide the "
        "audio file after {attempts} attempts. Please resend the voice message."
    ),
    "voice.transcription_failed": (
        "🎙 The voice message couldn't be recognized and didn't reach the "
        "session. Please send it again."
    ),
    "voice.transcribing": "🎙 Voice message is being transcribed…",
    "voice.queued_dropped": "Messages sent after it didn't reach the session either.",
    # Footer buttons
    "btn.stop": "⏹ Stop",
    "btn.kill": "💀 Kill",
    "btn.clear": "🧹 Clear",
    "btn.menu": "≡ Menu",
    "btn.term": "🖥 Term",
    "btn.back": "← Back",
    "btn.cancel": "× Cancel",
    "btn.login": "🔐 Log in",
    # Claude re-authentication (/login)
    "auth.expired": (
        "🔐 *Claude authorization expired*\n\n"
        "Every session on this host will keep failing until the login is "
        "renewed. The bot itself is fine — it can walk you through it.\n\n"
        "Send /login (or tap below): I hand you a link, you approve it in the "
        "browser and send the code back here."
    ),
    "auth.login.starting": "🔐 Starting the login exchange…",
    "auth.login.url": (
        "🔐 *Step 1/2* — open this and approve:\n\n"
        "{url}\n\n"
        "*Step 2/2* — the page shows a code. Send it here as a normal "
        "message. The link is valid for 15 minutes."
    ),
    "auth.login.no_url": (
        "❌ Could not get a login URL from the CLI. Try /login again; if it "
        "keeps failing, run `claude auth login` on the host."
    ),
    "auth.login.ok": (
        "✅ *Logged in.* Authorization renewed until {deadline}.\n\n"
        "Sessions that were failing will work on the next message."
    ),
    "auth.login.failed": "❌ The code was not accepted: {detail}\n\nSend /login to retry.",
    "auth.login.cancelled": "Login cancelled.",
    "auth.codex.device": (
        "🔐 *Codex sign-in*\n\n"
        "1. Open {url}\n"
        "2. Enter this code: `{code}`\n\n"
        "The bot will detect approval automatically; don't send the code here. "
        "The code is valid for about 15 minutes."
    ),
    "auth.codex.no_device_code": (
        "❌ Codex did not provide a device code. Check that a current Codex CLI "
        "is installed and run /login to retry."
    ),
    "auth.codex.ok": (
        "✅ *Codex is authorized.* You can create and resume sessions now."
    ),
    "auth.codex.failed": "❌ Codex sign-in failed: {detail}\n\nSend /login to retry.",
    "auth.codex.waiting": (
        "🔐 Codex is still waiting for browser approval. Use the link and code above."
    ),
    "auth.codex.required": (
        "🔐 Authorize Codex using the link above, then retry creating the session."
    ),
    "auth.codex.check_failed": (
        "❌ Could not check Codex authorization. Verify `codex --version` and "
        "`CODEX_COMMAND`, then send /login."
    ),
    "auth.codex.storage_mismatch": (
        "⚠ Codex found `auth.json`, but its effective credential storage does "
        'not read it. Set `cli_auth_credentials_store = "file"`; the bot '
        "will not replace the existing authorization."
    ),
    "btn.confirm": "✓ Confirm",
    "btn.no": "× No",
    "btn.yes_kill": "⚠ Yes, kill",
    "btn.yes_delete": "⚠ Yes, delete",
    "btn.yes_clear": "⚠ Yes, clear",
    "btn.refresh": "🔄 Refresh",
    "btn.save": "Saved",
    "btn.cancelled": "Cancelled",
    # Archive buttons
    "btn.restore": "⤴ Restore",
    "btn.restore_with_name": "⤴ Restore {name}",
    "btn.inspect": "🔍 Inspect",
    "btn.open_session": "📜 {name}",
    "btn.delete": "🗑 Delete",
    "btn.to_14d": "→ 14d",
    "btn.to_72h": "→ 72h",
    # More menu
    "mm.sessions": "📋 Sessions",
    "mm.status": "📊 Status",
    "mm.history": "📜 History",
    "mm.shot": "🧑‍💻 Shot",
    "mm.new": "🆕 New",
    "mm.archive": "🗄 Archive",
    "mm.settings": "⚙ Settings",
    # Menu screen body
    "menu.title": "*Menu*",
    "menu.empty": "*Menu*\n\nNo active session — pick one from the switcher or tap 🆕 New.",
    "menu.active": "*Menu* · active: *{name}*",
    # Settings — top
    "settings.title": "*Settings*",
    "settings.body": (
        "*Settings*\n\n"
        "Agent: `{agent}`\n"
        "Language: `{language}`\n"
        "Live lag: `{live_lag}s`\n"
        "Voice: `{voice}`\n\n"
        "_Tap a group to change._"
    ),
    # Settings — group labels (in the main grid)
    "settings.group.agent": "Agent",
    "settings.group.language": "Language",
    "settings.group.live_lag": "Live lag",
    "settings.group.voice": "Voice",
    # Settings — group sub-screen descriptions
    "settings.lag.body": (
        "*Live preview lag*\n\n"
        "Coalescing window for live-card edits.\n"
        "`0s` = update on every event, higher = quieter chat."
    ),
    "settings.voice.body": (
        "*Voice transcription*\n\n"
        "Backend used for voice messages.\n"
        "• `auto` — Apple on macOS, whisper.cpp elsewhere\n"
        "• `whisper` — force whisper.cpp\n"
        "• `apple` — force Apple Speech (macOS only)\n"
        "• `off` — drop voice messages"
    ),
    "settings.agent.body": (
        "*Agent*\n\n"
        "Global backend for the entire bot. All new sessions use either "
        "*Claude* or *Codex*.\n\n"
        "Switching is blocked while sessions from the current backend are "
        "still live. Archive or kill them first."
    ),
    "settings.lang.body": "*Language*\n\nUI language. Switches everything\nbut Claude's own output.",
    # Sessions list — only ``list.empty`` is still used (Menu → Sessions
    # empty-state when there's no active session). ``list.active`` /
    # ``list.lost`` are legacy.
    "list.empty": "No live sessions. Use 🆕 New to create one.",
    # Confirm dialogs
    "conf.kill": (
        "Kill *{name}*?\nTmux window dies, claude session id stored.\n"
        "Restore via the archive list."
    ),
    "conf.done": "Mark *{name}* as done?\nGoal closed, session archived.",
    "conf.delete": (
        "Delete *{name}* from archive?\nState record gone. JSONL kept on disk."
    ),
    "conf.clear": (
        "Clear *{name}*?\nSends Esc then /clear. Session context wiped — "
        "cannot be undone (unlike Kill → Restore)."
    ),
    "conf.killed": "💀 Killed `{name}`",
    "conf.done_ok": "🎉 Marked `{name}` as done.",
    "conf.deleted": "🗑 Archive entry deleted.",
    # Directory browser
    "dir.title": "*Select Working Directory*",
    "dir.current": "Current: `{path}`",
    "dir.empty": "_(No subdirectories)_",
    "dir.hint": "Tap a folder to enter, or select current directory",
    "dir.btn.up": "..",
    "dir.btn.select": "Select",
    # Session picker
    "picker.title": "*Resume Session?*",
    "picker.summary": "page {page}/{pages} — {total} session(s) in this directory.",
    "picker.btn.start_fresh": "🆕 Start fresh",
    "picker.btn.back_to_dirs": "← Back to dirs",
    # Inline toasts
    "toast.no_session": "No active session",
    "toast.window_gone": "Window gone",
    "toast.esc_sent": "⎋ Esc sent",
    "toast.cleared": "🧹 Context cleared",
    "toast.killed": "Killed",
    "toast.done": "Done",
    "toast.deleted": "Deleted",
    "toast.saved": "Saved",
    "toast.agent_live": (
        "Archive or kill all live sessions before switching the global agent."
    ),
    "toast.restored": "Restored",
    "toast.already_gone": "Already gone",
    "toast.nothing_to_kill": "Nothing to kill",
    "toast.term_opened": "🖥 Terminal opened",
    "toast.invalid_page": "Invalid page",
    "toast.session_not_found": "Session not found",
    "toast.restore_failed": "Restore failed: {msg}",
    "toast.range_14d": "→ 14d",
    "toast.range_72h": "→ 72h",
    # Archive screen
    "archive.title": "Archived sessions",
    "archive.range_72h": " (0–72h)",
    "archive.range_14d": " (0–14d)",
    "archive.empty": "No archived sessions in this window.",
    "archive.page_line": "page {page}/{pages} — {total} total",
    "archive.age.s": "{n}s ago",
    "archive.age.m": "{n}m ago",
    "archive.age.h": "{n}h ago",
    "archive.age.d": "{n}d ago",
    # /usage compact display
    "usage.title": "*Claude Code*",
    "usage.title.codex": "*OpenAI Codex*",
    "usage.unavailable": "Live usage unavailable.",
    "usage.auth_required": (
        "Codex authorization is required to load Usage. Complete the sign-in "
        "sent above, then refresh this screen."
    ),
    "usage.5h": "5h",
    "usage.week": "week",
    "usage.week_sonnet": "week (Sonnet)",
    "usage.not_reported": "not reported by Codex",
    "usage.today": "Today",
    "usage.today_left": "another",
    "usage.today_overspent": "over by",
    "usage.used": "Used",
    "usage.reset": "Reset",
    "usage.extra": "Extra",
    "usage.on": "on",
    "usage.off": "off",
    "usage.fetching": "Fetching usage…",
    # Settings group: weekly reset day
    "settings.group.weekly_reset_day": "Weekly reset",
    "settings.weeklyday.body": (
        "*Weekly reset day*\n\n"
        "Day of week the Anthropic weekly window resets.\n"
        "Used to compute the %/day burn rate on the weekly rows."
    ),
    "day.mon": "Mon",
    "day.tue": "Tue",
    "day.wed": "Wed",
    "day.thu": "Thu",
    "day.fri": "Fri",
    "day.sat": "Sat",
    "day.sun": "Sun",
    # Settings group: auto-approve interactive prompts
    "settings.group.auto_approve": "Auto-approve",
    "settings.approve.body": (
        "*Auto-approve*\n\n"
        "Bot's response to Claude Code's interactive Yes/No prompts\n"
        "that --dangerously-skip-permissions doesn't already bypass\n"
        "(e.g. WebFetch per-domain trust):\n"
        "• `off` — surface in chat, you tap manually\n"
        "• `on` — auto-Yes on every prompt"
    ),
    "approve.off": "off",
    "approve.on": "on",
    "settings.group.session_idle_hours": "Auto-archive after",
    "settings.idle_archive.body": (
        "*Session auto-archive*\n\n"
        "Archive a live session after this many hours without activity. "
        "Archived sessions remain available through Menu → Archive and can be restored."
    ),
    "settings.value.hours": "{value}h",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "off",
    "local.manual": "manual",
    "local.auto": "auto",
    # Settings group: how many recent end_turn boundaries to seed into a
    # fresh live card from the JSONL transcript.
    "settings.group.card_history": "Card history",
    "settings.cardhist.body": (
        "*Card history*\n\n"
        "How many recent end-of-turn boundaries to load into the live "
        "card on first access (after a bot restart, switcher tap, or "
        "Menu → Sessions). Deep history beyond this stays accessible "
        "via /history regardless of the chosen value.\n\n"
        "Higher = more scrollback in the card, more memory per session."
    ),
    "settings.group.card_page_lines": "Page size",
    "settings.pagesize.body": (
        "*Page size*\n\n"
        "Max lines on one card page. Older events drop to previous "
        "pages (◀); a long final answer is chunked across multiple "
        "pages with smart paragraph / sentence boundaries — no breaks "
        "mid-word. ±5 lines tolerance.\n\n"
        "Smaller = compact phone view. Larger = more context per page "
        "but heavier message edits."
    ),
    "settings.group.card_inline_screenshots": "Inline screenshots",
    "settings.screens.body": (
        "*Inline screenshots*\n\n"
        "When *on*, the terminal pane appears only while the turn is "
        "*RUNNING*: body → gap → pane → gap → context → background. "
        "It disappears on *IDLE*, final answer, or /clear, and returns "
        "when the next turn starts. Pane changes are throttled to ~3 sec.\n\n"
        "Rich Bot API keeps text and media in one message; older servers "
        "use photo + caption. Failed sends fall back to legacy photo, then "
        "text-only. Transient edits retry on the next update; a lost card "
        "is recreated without an immediate duplicate.\n\n"
        "A silent unfinished active turn keeps the pane without a warning "
        "push; a background one is marked only with ⚠️ in the background panel.\n\n"
        "When *off*, the card keeps its normal text-only flow and Shot "
        "remains available from the top-row terminal button."
    ),
    "screens.on": "on",
    "screens.off": "off",
    # Bg notifications (Task #42) — three independent toggles.
    "settings.group.bg_notify_finished": "Bg: task complete",
    "settings.group.bg_notify_error": "Bg: errors",
    "settings.group.bg_notify_needs_action": "Bg: needs action",
    "settings.bg_notify.finished.body": (
        "*Bg session: task complete*\n\n"
        "When a background session reaches end-of-turn, push a quiet "
        "notification ✅ [<name>] task complete so you can switch in."
    ),
    "settings.bg_notify.error.body": (
        "*Bg session: errors*\n\n"
        "Push ❌ [<name>] error when a background session emits an "
        "error event. (Currently fires only on explicit error events; "
        "exception detection is being extended.)"
    ),
    "settings.bg_notify.needs_action.body": (
        "*Bg session: needs action*\n\n"
        "Push ❓ [<name>] needs your attention when a background session "
        "shows an AskUserQuestion / ExitPlanMode / Permission prompt. "
        "Otherwise only the ❓ badge in the bg-panel signals it — easy "
        "to miss."
    ),
    "settings.group.haiku_naming": "AI session names",
    "settings.haiku.body": (
        "*AI session names*\n\n"
        "When *on*, every new session is renamed after the first user "
        "message ≥20 chars via a one-shot lightweight-model call (Haiku "
        "for Claude, `CODEX_NAMING_MODEL` for Codex) — yields a 1-3 "
        "word kebab-case summary of the session's intent "
        "(``token-budget-alerts``, ``archive-pagination-fix``). "
        "Manually-renamed sessions (``/rename``, ``/new "
        "<name>``) are never overwritten.\n\n"
        "When *off*, sessions keep the directory-basename name forever "
        "(``workdir``, ``workdir-2``, ``ccbot``). Zero token cost."
    ),
    # Settings categories (top-level Settings is now a category selector).
    "settings.cat.card": "🃏 Card / view",
    "settings.cat.notifications": "🔔 Notifications",
    "settings.cat.voice": "🎙 Voice",
    "settings.cat.terminal": "🖥 Local terminal",
    "settings.cat.behavior": "⚙ Agent, behavior & language",
    "settings.cat.card.body": (
        "*Card / view*\n\nLayout, density and refresh of the live session card."
    ),
    "settings.cat.notifications.body": (
        "*Notifications*\n\n"
        "Bg-session pushes (finished / errors / needs-action) and "
        "the weekly-reset day for quota alerts."
    ),
    "settings.cat.voice.body": (
        "*Voice*\n\nSpeech-to-text backend for incoming voice messages."
    ),
    "settings.cat.terminal.body": (
        "*Local terminal*\n\n"
        "Native Terminal / iTerm window attached to each new session."
    ),
    "settings.cat.behavior.body": (
        "*Behavior & language*\n\n"
        "Global agent; auto-approve prompts; Haiku session names; UI language."
    ),
    # Settings group: pop a native Terminal/iTerm window per new session
    "settings.group.local_terminal": "Local terminal",
    "settings.local.body": (
        "*Local terminal*\n\n"
        "Optional native desktop terminal attached to a session's "
        "tmux window — useful for driving Claude by hand in parallel "
        "with the Telegram UI.\n\n"
        "*off* — never spawn, never offer.\n"
        "*manual* — no auto-spawn; *🖥 Term* shows up next to *Stop / "
        "Kill / Clear / Menu* whenever the active session has no "
        "terminal attached.\n"
        "*auto* — spawn one on every new session AND show the same "
        "*🖥 Term* button whenever no terminal is attached.\n\n"
        "macOS: Terminal.app or iTerm2 (auto-detected).\n"
        "Linux: pick an emulator below. Tap *Configure via Claude* if "
        "the auto-detected list is wrong for your setup."
    ),
    "settings.local.claude_help": "🪄 Configure via Claude",
    # /help inline mini-doc
    "help.home.body": (
        "*Help*\n\n"
        "ccbot bridges this DM to N parallel Claude Code sessions running "
        "in tmux. Tap a section below for a quick tour."
    ),
    "help.btn.overview": "Overview",
    "help.btn.sessions": "Sessions",
    "help.btn.menu": "Menu",
    "help.btn.commands": "Commands",
    "help.btn.voice": "Voice & files",
    "help.btn.alerts": "Alerts",
    "help.btn.terminal": "Local terminal",
    "help.btn.tips": "Tips",
    "help.body.overview": (
        "*Overview*\n\n"
        "One private DM, many parallel Claude Code sessions. Send any "
        "text — it goes to your *active* session. Each session lives in "
        "its own tmux window with its own claude process; switching the "
        "active session never pauses the others.\n\n"
        "The inline keyboard under the most recent bot message hosts "
        "the session switcher and the ≡ Menu surface."
    ),
    "help.body.sessions": (
        "*Sessions*\n\n"
        "• *Create.* Send any text from an empty DM, or tap ≡ Menu → 🆕 "
        "New, then pick a project directory.\n"
        "• *Switch.* Tap a session button in the inline switcher under "
        "the latest bot message.\n"
        "• *Reply-quote.* Reply to a non-active session's bot message — "
        "your text is routed there for that one message only.\n"
        "• *Done.* `/done [name]` archives a session as completed.\n"
        "• *Idle TTL.* Sessions auto-archive after the selected 6/12/24h without activity.\n"
        "• *Restore.* ≡ Menu → 📦 Archive → tap *Restore*."
    ),
    "help.body.menu": (
        "*≡ Menu*\n\n"
        "Open via /menu or the ≡ Menu inline button. Items:\n"
        "• 📋 *Sessions* — jump to the active session's live card\n"
        "• 📊 *Status* — Claude Code 5h / weekly / sonnet quotas\n"
        "• 🧑‍💻 *Shot* — terminal snapshot of the active session\n"
        "• 🆕 *New* — create a session from a directory browser\n"
        "• 📦 *Archive* — restore / inspect / delete archived sessions\n"
        "• ⚙ *Settings* — grouped by Card / Notifications / Voice / "
        "Terminal / Behavior."
    ),
    "help.body.commands": (
        "*Slash commands*\n\n"
        "Bot-side:\n"
        "• `/menu` — open the inline menu\n"
        "• `/help` — this help\n"
        "• `/done [name]` — archive a session\n"
        "• `/health` — uptime, queues, latency, counters\n\n"
        "Claude Code passthrough — any other `/cmd` is forwarded:\n"
        "• `/model` `/effort` `/clear` `/compact` `/cost` `/memory` …\n\n"
        "Type a leading `!` to capture local shell output and forward."
    ),
    "help.body.voice": (
        "*Voice & files*\n\n"
        "• *Voice.* Send a voice message — transcribed locally "
        "(whisper.cpp / Apple Speech) and routed to the active session "
        "as if you typed it.\n"
        "• *Photo / document.* Lands in `<workdir>/.ccbot-inbox/` and "
        "Claude is told via the relative path (with your caption prefix "
        "if you attached one). Files auto-clean after 24h; the Telegram "
        "`file_id` is retained for 30d for `/restore-file`."
    ),
    "help.body.alerts": (
        "*Alerts*\n\n"
        "*Quota alerts.* 5h / weekly / weekly-Sonnet quotas are sampled "
        "from the live `/usage` modal every 10 min. Bot pushes when % "
        "crosses 50, 75, or 90.\n\n"
        "*Bg session pushes.* Settings → Notifications has three "
        "toggles (all default on):\n"
        "• ✅ task complete\n"
        "• ❌ error\n"
        "• ❓ needs your attention (interactive prompt)\n"
        "Active session never pushes — it edits its live card instead.\n\n"
        "*Context fill.* The card shows ``context: N%`` per session. "
        "For Codex it uses exact token usage and model-window values from "
        "the rollout. For Claude it is a JSONL estimate (latest assistant "
        "input + cache reads vs the published model window)."
    ),
    "help.body.terminal": (
        "*Local terminal*\n\n"
        "Settings → Local terminal: when *on*, every new session pops "
        "a native window already attached to its tmux window — drive "
        "the session by hand from the desktop in parallel.\n\n"
        "macOS: Terminal.app / iTerm2 (auto, prefers iTerm tabs).\n"
        "Linux: pick an emulator from the auto-detected list, or use "
        "*Configure via Claude* for unusual setups.\n\n"
        "Direct attach also works any time: `tmux attach -t ccbot`."
    ),
    "help.body.tips": (
        "*Tips*\n\n"
        "• *Auto-approve.* Settings → Auto-approve auto-Yes's "
        "interactive prompts that --dangerously-skip-permissions "
        "doesn't already bypass (e.g. WebFetch domain trust).\n"
        "• *Card edit lag.* Settings → Live lag controls how often the "
        "live session card is re-edited (lower = snappier, higher = "
        "less rate-limit pressure).\n"
        "• *Languages.* Settings → Language: en / ru / zh.\n"
        "• *Outbound proxy.* Set `TG_PROXY_URL` if the host can't reach "
        "api.telegram.org directly.\n"
        "• *Single instance.* Bot holds an exclusive flock on "
        "`$CCBOT_DIR/ccbot.lock`; a second `uv run ccbot` refuses with "
        "an error in stderr instead of fighting for Telegram updates.\n"
        "• *Hook self-heal.* `SessionStart` + `UserPromptSubmit` hooks "
        "both update `session_map.json` — a missed SessionStart is "
        "fixed on the next prompt automatically."
    ),
}

__all__ = ["EN"]
