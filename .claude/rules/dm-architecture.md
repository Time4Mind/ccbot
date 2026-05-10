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
  -> for each user with active_session whose Session.claude_session_id == S:
       enqueue message to user
  -> background sessions of the same user emit notifications via their own
     "live cards" (edit-in-place) plus push messages on key events
```

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

### Context preview

When a session button is tapped, the same message is edited in place to show a preview of the selected session: header (name, state, usage), last user message (`PREVIEW_USER_LINES`), last assistant message (`PREVIEW_ASSISTANT_LINES`), and last tools (`PREVIEW_TOOLS`). After the click, if the previewed session emits new bot-side events, the preview message is `editMessageText`-updated, coalesced at most once per `PREVIEW_LIVE_LAG` seconds. `PREVIEW_LIVE_LAG=0` disables live updates.

### Per-session live card (C7)

Each session has one "live card" message in chat, which the bot keeps editing. The card carries the latest tool/event one-line summary plus the final result on completion or error. New card is opened on session completion or error.

### Push notifications (C5)

Sent via `send_message` only on completion, error, and `AskUserQuestion`. Format: `<color-emoji> [<session-name>] <message>`. Color emoji is hashed from the session name.

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

## What is unchanged

- `tmux_manager`, `transcript_parser`, `terminal_parser`, `screenshot`, `hook`, `monitor_state`, `markdown_v2`, `telegram_sender`.
- `session_map.json` semantics (keyed by `tmux_session:window_id`, written by Claude Code SessionStart hook).
- `MarkdownV2` formatting pipeline.
- Per-user message queue and rate limiting (`AIORateLimiter`).
- Tool-use / tool-result pairing (in-place edit).
