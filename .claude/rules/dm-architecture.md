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

Written by both the `SessionStart` and `UserPromptSubmit` hooks to `session_map.json`. SessionStart catches every new claude process; UserPromptSubmit fires per prompt and self-heals the mapping if it diverges from the pane's current `session_id` (covers `/resume`, `/clear`, and the bot-restart-race window where SessionStart was missed). `WindowState.session_id` mirrors the current map.

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
  -> for every allowed user (the session pool is global — see
     ``all_user_sessions_with_claude_id``), for each of their
     Sessions with claude_session_id == S:
       active session  -> enqueue + paint the user's live card
       background sess -> update handlers.bg_status (status enum +
                          needs_action snapshot); optionally one
                          short push on a state transition
```

Background sessions have **no live card of their own** — no in-chat
card edits, no AskUserQuestion prompt surfacing. Their state surfaces
as a panel at the bottom of the active session's card via
``handlers.bg_status.render_panel``, plus (opt-out) a one-line push on
transition. See "Background-session panel" below.

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
- `lost`: tmux window vanished externally. Surfaces in the switcher with a Restore button.

Goal closure is done only by the user via `/done <session>`. The bot never auto-closes a goal.

## UI rules

### Switcher (A8)

Inline keyboard with one button per active session, plus a `+ new` button. The switcher is appended to **the most recent** bot content message only. When a new bot message is sent that should carry the switcher, the previous switcher's reply markup is stripped via `editMessageReplyMarkup` to avoid duplicate switchers in the chat.

State for "where the live switcher currently lives" is held in memory and persisted as `last_switcher_msg_id: dict[user_id, message_id]` in state.json.

**Button order — oldest → newest.** `build_switcher_keyboard` re-sorts by `(created_at, id)` instead of using the order `list_user_sessions` returns (active-first, then by name). A session therefore keeps a stable slot for its lifetime and a new one appends to the right. Two consequences worth knowing:

- `SessionManager.set_session_window` (restore-from-archive, or re-binding a `lost` session) bumps `created_at` to now, so a restored session re-enters at the newest end rather than in its original chronological slot.
- Every surface that renders session buttons applies the same sort. `bot/commands/info.py: build_screenshot_compact_keyboard` sorts explicitly for this reason — it builds its rows straight from `list_user_sessions`, so without the sort a session would sit in a different slot under `/screenshot` than on the live card. Any future switcher-like surface must do the same; `list_user_sessions` itself is deliberately left alone.

### Footer button order

The main / live-card view's footer keyboard is built in `handlers.menu.build_footer_keyboard` with `screen="main"`:

```
[Stop/Kill, Clear, 🧑‍💻 Shot, (Open Terminal)]   ← top: per-session controls
[switcher buttons row(s)]                       ← middle
[+ new] [≡ Menu]                                ← anchored bottom row
```

`+ new` and `≡ Menu` share a single row so the two "go-elsewhere" affordances sit side-by-side. The same slot pairs `[+ new] [Back]` in the Menu → Sessions empty state, and a single `Back` button in `/archive` / Settings sub-screens. `build_switcher_keyboard` takes an `include_new: bool = True` flag — passed `False` by `build_footer_keyboard(screen="main")` so it can compose the bottom pair itself.

### Switcher tap → history view

When the user taps a session button in the main switcher:

1. `transfer_card_to_carrier` pauses the FROM session's card and claims the carrier message_id for the TO session.
2. `set_active_session(user, target)` flips the routing pointer.
3. If the TO session has a stashed `bg_status.pending_interactive_ui` *and* the live pane still shows the prompt, the carrier is claimed as the live card and flipped into kb-mode (`enter_kb_mode`) so the CB_ASK_* keyboard drives the prompt. Otherwise the carrier is painted as the session's live card (`paint_card_on_carrier` — header + paginated body + bg-panel + footer) and receives subsequent claude events in place.
4. `bg_status.mark_seen` + `prune_seen` drop the just-viewed badge from the panel.

Pagination (`CB_HISTORY_PREV/NEXT`) walks the card's own pages and keeps the main footer under them. There is no explicit "History" button in the footer — pagination buttons themselves are the navigation affordance, and the user lands on the paginated view via switcher tap, Menu → Sessions, or `/screenshot` → `Back`.

Menu → Sessions (`CB_MM_LIST`) is **not** a separate list screen: it paints the active session's live card onto the carrier (`paint_card_on_carrier`), because that card already carries the switcher row and in-card pagination. Only the no-active-session case renders a thin empty state with `[+ new] [Back]`.

### Per-session live card

Each active session has one "live card" message in chat, which the bot keeps editing. The card carries the latest tool/event one-line summary plus the final result on completion or error. A fresh card pre-seeds itself from the session's JSONL with the last `card_history` end-of-turn boundaries (`_ensure_seeded` → `_seed_events_from_jsonl`; the user setting defaults to `CARD_SEED_TURNS = 20`). New card is opened on session completion, error, stale pause, or overflow.

The active session's card body ends with the bg-status panel block (see below). Card edits coalesce within `CARD_EDIT_LAG`.

### Background-session panel

`handlers.bg_status` keeps a per-user, per-session map of:

- `status`: `working` ⏳ / `finished` ✅ / `error` ❌ / `needs_action` ❓
- `quota_level`: `none` / `green` / `yellow` / `red` — **dormant**: the field
  exists (and is tolerated on deserialize) but nothing currently sets it, so
  the `⚠️🟢/🟡/🔴` quota glyph does not render. Its driving config
  (`BG_STATUS_QUOTA_THRESHOLDS`) was removed as dead. (Quota *alerts* —
  push messages on 5h/weekly band crossings — are a separate, live feature
  in `handlers/quota_alerts.py`.)
- `seen`: True once the user tapped the session in the switcher post-finalisation
- `pending_interactive_ui`: snapshot `(content, ui_name)` for bg sessions that have an AskUserQuestion / ExitPlanMode / permission prompt waiting

`render_panel(user_id, active_session_id)` formats the block appended to the bottom of the active card. `BG_STATUS_MAX` caps visible badges; older rows collapse to `+N more`.

A bg session also pushes a one-liner (`push_event`) when
`bg_status.update_status` reports an actual *transition* — the return
value is the dedup, so a re-affirmed state never re-pushes. Three
independent user settings gate it, all defaulting to `True`:
`bg_notify_finished`, `bg_notify_error`, `bg_notify_needs_action`
(`bot/session_events.py`). With all three off, background sessions are
completely silent and only the panel badge changes.

### Push notifications

Reserved for events that genuinely cannot be deferred to a card edit:
- task completion in an active session (now folded into the card body itself with a `(task complete)` footer; no separate push)
- blocker errors
- AskUserQuestion / ExitPlanMode for the **active** session (rendered as a dedicated message with arrow / Enter / Esc keyboard)
- session lifecycle (`created` / `restored` / `archived` / `done` / `killed`)
- inbox file received
- a **background** session's state transition into finished / error /
  needs_action — one short line, gated per-type by the `bg_notify_*`
  user settings (default on)

### Typing indicator

`bot.send_chat_action(TYPING)` is fired from `session_events.handle_new_message` once per inbound claude event for the **active** session. Telegram's ~5s indicator window means a steadily-emitting session keeps "typing…" alive; an idle session lets it fade. Bg sessions skip — only the foreground's busy state surfaces in the chat header.

## Slash commands (B7)

Only a few commands are published via `setMyCommands` (the Telegram
`/`-menu, `bot/app.py`); the rest are registered handlers that work
when typed but stay out of the menu. There is **no** `/list`, `/use`,
or `/rename` — the inline switcher / Menu → Sessions replace them.

Published:

```
/menu     Open the inline Menu surface
/help     Quick guide / inline doc
/history  Full transcript of the active session
/done     Mark a session as done
```

Plus the forwarded Claude Code pickers `/model` `/effort` `/compact`
`/memory` when present in `CC_COMMANDS`.

Hidden (registered, typed-only): `/new` `/kill` `/stop` `/archive`
`/screenshot` `/usage` `/health` `/login`.

`/login` is the recovery path for a dead Claude OAuth login: the bot spawns
`claude auth login` on a pty, posts its URL, and consumes the user's next
message as the pasted code (`bot/commands/auth.py`). It stays out of the
published menu because it is surfaced by the "authorization expired" notice
itself. The bot needs no Claude auth of its own, so this works while every
session is failing.

Detection is gated on Claude Code's **own** error flag, not on wording:
`claude_auth.is_auth_failure_event(msg.api_error, msg.text)`. The CLI writes a
dead login as a synthetic assistant turn carrying `isApiErrorMessage: true` and
`error: "authentication_failed"`, which `transcript_parser` stamps onto
`ParsedEntry.api_error` and `session_monitor` carries as `NewMessage.api_error`.
Matching the error *wording* against event text was tried first and had to be
reverted: any session that merely discussed the failure (this feature's own
development session did) made the bot announce that a healthy host had lost its
login. `session_events` pushes the notice once per credential deadline
(`_notified_walls`), with a 🔐 button.

On success `maybe_consume_code` deletes the user's message (the code is a
single-use credential), reports the new deadline, and then calls
`_restore_working_surface`: the active session's card is reposted below the
confirmation so the switcher and footer are immediately reachable, or the Menu
screen is sent when no session is active. A bare confirmation leaves the last
card buried above the notice / link / code exchange.

The legacy ``/status`` command was retired — Menu → Status fetches
the same /usage modal data via the dedicated ``ccbot-usage`` window.

## What does not exist in DM mode

- `thread_bindings` - removed.
- `bind_thread`, `unbind_thread`, `get_window_for_thread`, `resolve_window_for_thread`, `iter_thread_bindings` - removed.
- `on_topic_closed`, `on_topic_edited`, `on_topic_deleted` - removed.
- `group_chat_ids` - removed (DM is the only chat, `chat_id == user_id`).
- `setMyCommands` is published once on startup, not per-topic.

## User-msg disposition

The `card_position` setting was retired. There is now one canonical
behaviour: every inbound user message (text / voice / photo / document)
triggers ``notifications.repost_card`` — the live card is re-sent as a
new message below the user's text and the previous card msg is dropped.

## Per-session context fill

The live card and bg-status panel display ``context: N%`` per session.
The value is computed in ``usage.context_pct_for_session`` from the
session's JSONL — latest assistant turn's
``input_tokens + cache_creation_input_tokens + cache_read_input_tokens``
divided by the model's published context window
(``_budget_for_model``: 1 M for opus-4-7 / opus-4-6 / sonnet-4-6,
200 k for everything else). The number is refreshed in
``session_events.handle_new_message`` on every assistant end-of-turn
text turn and stashed on ``CardState.context_pct`` /
``bg_status.BgStatus.context_pct``.

The result is an *approximation* of what Claude Code's own
``/context`` modal reports — typically within ±10 % relative. The two
diverge because /context additionally counts system prompt / tools /
memory files / autocompact buffer that are not always reflected in the
last assistant turn's ``cache_read``. Sending /context into a live
pane was tried but rejected: Claude Code records the modal output as a
fake user turn in JSONL, polluting the live card and eating tokens.

## What is unchanged

- `tmux_manager`, `transcript_parser`, `terminal_parser`, `screenshot`, `hook`, `monitor_state`, `markdown_v2`, `telegram_sender`.
- `session_map.json` semantics (keyed by `tmux_session:window_id`, written by Claude Code `SessionStart` + `UserPromptSubmit` hooks).
- `MarkdownV2` formatting pipeline.
- Per-user message queue and rate limiting (`AIORateLimiter`).
- Tool-use / tool-result pairing (in-place edit).
