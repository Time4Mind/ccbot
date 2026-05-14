# DM Multi-Session Architecture

The bot operates exclusively in a private 1-1 Telegram DM with a single user. There is no supergroup, no forum topics, no thread routing. All routing keys on a single `active_session` per user, plus an inline switcher for parallel sessions.

Authoritative product spec: `doc/dm-multisession-spec.md`. Implementation plan: `doc/dm-multisession-plan.md`.

## Routing model

```
+-------------+     +----------------+     +-----------+     +-------------+
| TG user_id  | --> | active_session | --> | window_id | --> | session_id  |
| (single)    |     | per user       |     | (tmux)    |     | (Claude)    |
+-------------+     +----------------+     +-----------+     +-------------+
                     active_sessions       window_states     session_map.json
                     (state.json)          (state.json)      (written by hook)
```

There is no `thread_id`. There is no `message_thread_id` parameter on outbound messages. All `_get_thread_id()` reads return `None` in DM and are deprecated.

## Mappings

### user_id -> session.id (active_sessions)

```python
# session.py: SessionManager
active_sessions: dict[int, str]  # user_id -> Session.id (short id)
sessions: dict[str, Session]     # short id -> Session record
```

- `active_sessions` is the routing key for inbound user text.
- `sessions` holds full per-session metadata: goal, window_id, workdir, state, claude_session_id, timestamps, last_event, token_usage.
- Persisted to `state.json` atomically.

### Session.id -> window_id

`Session.window_id` is the tmux window id (for example `@5`). One session occupies one tmux window for its lifetime in active state. On archive the window is killed; on restore a new window is created and `claude --resume <claude_session_id>` is run.

### window_id -> claude session_id

Unchanged. Still written by the `SessionStart` hook to `session_map.json`. `WindowState.session_id` mirrors that.

## Message flows

### Inbound (user -> Claude)

```
User sends text in DM
  -> session_id = session_manager.get_active_session(user.id)
  -> session = session_manager.sessions[session_id]
  -> send_to_window(session.window_id, text)
```

### Outbound (Claude -> user)

```
SessionMonitor reads new event for claude session_id S
  -> for each user whose Session.claude_session_id == S:
       active session  -> enqueue + paint the user's live card
       background sess -> silent: only update handlers.bg_status
                          (status enum + quota_level + needs_action snapshot)
```

Background sessions emit **no** Telegram messages of their own — no
live-card edits, no push, no AskUserQuestion prompt surfacing. Their
state surfaces only as a panel at the bottom of the active session's
card via ``handlers.bg_status.render_panel``. See "Background-session
panel" below.

### One-shot reply-quote routing

When the user replies (Telegram native quote) to a bot message that belongs to non-active session N, the reply text is routed to N for that single message. Active session does not change.

## Session lifecycle

```
[create] -> active -> idle (no input >= SESSION_IDLE_TTL) -> archived
            ^                                                  |
            +-------------- restore --------------------------+
            
archived -> [purged after 14d in archive]
```

States:

- `active`: tmux window alive, claude process running, in switcher.
- `idle`: tmux window alive, no input from user for >= SESSION_IDLE_TTL. Promoted to `archived` after the same threshold.
- `archived`: tmux window killed. `claude --resume` rehydrates on restore. Visible in `/archive`.
- `completed`: archived via `/done`. Tagged for the user; otherwise identical to `archived`.
- `lost`: tmux window vanished externally. Surfaces in `/list` with a Restore button.

Goal closure is done only by the user via `/done <session>`. The bot never auto-closes a goal.

## UI rules

### Switcher (A8)

Inline keyboard with one button per active session, plus a `+ new` button. The switcher is appended to **the most recent** bot content message only. When a new bot message is sent that should carry the switcher, the previous switcher's reply markup is stripped via `editMessageReplyMarkup` to avoid duplicate switchers in the chat.

State for "where the live switcher currently lives" is held in memory and persisted as `last_switcher_msg_id: dict[user_id, message_id]` in state.json.

### Footer button order

The main / live-card view's footer keyboard is built in `handlers.menu.build_footer_keyboard` with `screen="main"`:

```
[Stop/Kill, Clear, 🧑‍💻 Shot, (Open Terminal)]   ← top: per-session controls
[switcher buttons row(s)]                       ← middle
[+ new] [≡ Menu]                                ← anchored bottom row
```

`+ new` and `≡ Menu` share a single row so the two "go-elsewhere" affordances sit side-by-side. The same slot pairs `[+ new] [Back]` in `/list`, and a single `Back` button in `/archive` / Settings sub-screens. `build_switcher_keyboard` takes an `include_new: bool = True` flag — passed `False` by `build_footer_keyboard(screen="main")` and by `build_list_view` so they can compose the bottom pair themselves.

### Switcher tap → history view

When the user taps a session button in the main switcher:

1. `transfer_card_to_carrier` pauses the FROM session's card and claims the carrier message_id for the TO session.
2. `set_active_session(user, target)` flips the routing pointer.
3. If the TO session has a stashed `bg_status.pending_interactive_ui` *and* the live pane still shows the prompt, the carrier is repainted with the prompt + the regular CB_ASK_* keyboard (`adopt_interactive_msg`). Otherwise the carrier is painted with `send_history` — the full paginated transcript view, with the standard footer ridden along as `extra_rows` so management controls stay reachable.
4. `bg_status.mark_seen` + `prune_seen` drop the just-viewed badge from the panel.

Pagination (`CB_HISTORY_PREV/NEXT`) preserves the original `extra_rows` by stamping `context.user_data['_history_origin']` (`switcher` or `menu_list`) when the history view is first painted; the pagination handler rebuilds the matching footer from this hint. There is no explicit "History" button in the footer — pagination buttons themselves are the navigation affordance, and the user lands on the paginated view via switcher tap, Menu → List, or `/screenshot Back` (both `m` and `l` origins now paint history).

Tapping a session in the `/list` view (Menu → List) instead re-renders the list view with the new active highlighted — the management surface is the more useful affordance in that context. Tracked via `context.user_data['_in_list_view']`, cleared on `CB_MM_BACK`, `CB_FT_MORE`, and any typed message.

### Per-session live card

Each active session has one "live card" message in chat, which the bot keeps editing. The card carries the latest tool/event one-line summary plus the final result on completion or error. A fresh card pre-seeds itself with up to `CARD_PRIOR_CONTEXT` transcript entries from before the user's most recent message (`_seed_prior_context_lines`). New card is opened on session completion, error, stale pause, or overflow.

The active session's card body ends with the bg-status panel block (see below). Card edits coalesce within `CARD_EDIT_LAG`.

### Background-session panel

`handlers.bg_status` keeps a per-user, per-session map of:

- `status`: `working` ⏳ / `finished` ✅ / `error` ❌ / `needs_action` ❓
- `quota_level`: `none` / `green` / `yellow` / `red` — sticky upward, drives `⚠️🟢/🟡/🔴`
- `seen`: True once the user tapped the session in the switcher post-finalisation
- `pending_interactive_ui`: snapshot `(content, ui_name)` for bg sessions that have an AskUserQuestion / ExitPlanMode / permission prompt waiting

`render_panel(user_id, active_session_id)` formats the block appended to the bottom of the active card. `BG_STATUS_MAX` caps visible badges; older rows collapse to `+N more`.

Bg sessions never emit push notifications. Token-threshold crossings flip the quota glyph instead of pushing. The active session's header carries the same glyph when its own quota crosses.

### Push notifications

Reserved for events that genuinely cannot be deferred to a card edit:
- task completion in an active session (now folded into the card body itself with a `(task complete)` footer; no separate push)
- blocker errors
- AskUserQuestion / ExitPlanMode for the **active** session (rendered as a dedicated message with arrow / Enter / Esc keyboard)
- session lifecycle (`created` / `restored` / `archived` / `done` / `killed`)
- inbox file received

Bg sessions never push, period.

### Typing indicator

`bot.send_chat_action(TYPING)` is fired from `session_events.handle_new_message` once per inbound claude event for the **active** session. Telegram's ~5s indicator window means a steadily-emitting session keeps "typing…" alive; an idle session lets it fade. Bg sessions skip — only the foreground's busy state surfaces in the chat header.

## Slash commands (B7)

Published via `setMyCommands` on startup so they appear in the Telegram `/`-menu:

```
/new      Create a new session (with optional name and path)
/list     List active sessions with state and usage
/use      Switch active session
/rename   Rename a session
/kill     Stop and archive a session immediately
/stop     Send Esc to interrupt current task in active session
/done     Mark goal as achieved and archive
/archive  Browse archived sessions (paginated)
/status   Detailed usage breakdown
```

## What does not exist in DM mode

- `thread_bindings` - removed.
- `bind_thread`, `unbind_thread`, `get_window_for_thread`, `resolve_window_for_thread`, `iter_thread_bindings` - removed.
- `on_topic_closed`, `on_topic_edited`, `on_topic_deleted` - removed.
- `group_chat_ids` - removed (DM is the only chat, `chat_id == user_id`).
- `setMyCommands` is published once on startup, not per-topic.

## User settings: card_position

`Settings → Card position` (`context.user_data['card_position']`, persisted in `state.json` under `user_settings`) controls how the user's outgoing text relates to the live card. Applied at the end of `text_handler`:

- `push` (default): leave the user's message; the live card scrolls up naturally.
- `delete`: bot deletes the user's message after dispatch so the card stays the most recent message.
- `repost`: bot re-sends the live card as a new message below the user's text and drops the previous card message (`notifications.repost_card`).

## What is unchanged

- `tmux_manager`, `transcript_parser`, `terminal_parser`, `screenshot`, `hook`, `monitor_state`, `markdown_v2`, `telegram_sender`.
- `session_map.json` semantics (keyed by `tmux_session:window_id`, written by Claude Code SessionStart hook).
- `MarkdownV2` formatting pipeline.
- Per-user message queue and rate limiting (`AIORateLimiter`).
- Tool-use / tool-result pairing (in-place edit).
