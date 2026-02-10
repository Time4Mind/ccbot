# Topic-Only Architecture

The bot operates exclusively in Telegram Forum (topics) mode. There is **no** `active_sessions` mapping, **no** `/list` command, **no** General topic routing, and **no** backward-compatibility logic for older non-topic modes. Every code path assumes named topics.

## 1 Topic = 1 Window = 1 Session

```
┌─────────────┐      ┌─────────────┐      ┌─────────────┐
│  Topic ID   │ ───▶ │ Window Name │ ───▶ │ Session ID  │
│  (Telegram) │      │   (tmux)    │      │  (Claude)   │
└─────────────┘      └─────────────┘      └─────────────┘
     thread_bindings      session_map.json
     (state.json)         (written by hook)
```

## Mapping 1: Topic → Window (thread_bindings)

```python
# session.py: SessionManager
thread_bindings: dict[int, dict[int, str]]  # user_id → {thread_id → window_name}
```

- Storage: memory + `state.json`
- Written when: user creates a new session via the directory browser in a topic
- Purpose: route user messages to the correct tmux window

## Mapping 2: Window → Session (session_map.json)

```python
# session_map.json (key format: "tmux_session:window_name")
{
  "ccbot:project": {"session_id": "uuid-xxx", "cwd": "/path/to/project"},
  "ccbot:project-2": {"session_id": "uuid-yyy", "cwd": "/path/to/project"}
}
```

- Storage: `session_map.json`
- Written when: Claude Code's `SessionStart` hook fires
- Property: one window maps to one session; session_id changes after `/clear`
- Purpose: SessionMonitor uses this mapping to decide which sessions to watch

## Message Flows

**Outbound** (user → Claude):
```
User sends "hello" in topic (thread_id=42)
  → thread_bindings[user_id][42] → "project"
  → send_to_window("project", "hello")
```

**Inbound** (Claude → user):
```
SessionMonitor reads new message (session_id = "uuid-xxx")
  → Iterate thread_bindings, find (user, thread) whose window maps to this session
  → Deliver message to user in the correct topic (thread_id)
```

**New topic flow**: First message in an unbound topic → directory browser → select directory → create window → bind topic → forward pending message.

**Topic lifecycle**: Closing/deleting a topic auto-kills the associated tmux window and unbinds the thread. Stale bindings (window deleted externally) are cleaned up by the status polling loop.

## Session Lifecycle

**Startup cleanup**: On bot startup, all tracked sessions not present in session_map are cleaned up, preventing monitoring of closed sessions.

**Runtime change detection**: Each polling cycle checks for session_map changes:
- Window's session_id changed (e.g., after `/clear`) → clean up old session
- Window deleted → clean up corresponding session
