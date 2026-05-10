# CLAUDE.md

ccbot (this fork) — Telegram bot that bridges a private 1-1 DM to multiple parallel Claude Code sessions via tmux windows. One user, N sessions, one inline switcher in the most recent bot message.

Authoritative product spec: `doc/dm-multisession-spec.md`. Implementation plan: `doc/dm-multisession-plan.md`.

Tech stack: Python, python-telegram-bot, tmux, uv.

## Common Commands

```bash
uv run ruff check src/ tests/         # Lint — MUST pass before committing
uv run ruff format src/ tests/        # Format — auto-fix, then verify with --check
uv run pyright src/ccbot/             # Type check — MUST be 0 errors before committing
./scripts/restart.sh                  # Restart the ccbot service after code changes
ccbot hook --install                  # Auto-install Claude Code SessionStart hook
```

## Core Design Constraints

- **DM-only (private 1-1 chat)** — no supergroup, no topics. Routing keyed by `active_sessions: dict[user_id -> session_id]` plus `Session.window_id`.
- **Parallel sessions** — switching the active session never pauses or stops work in other sessions. Each session has its own tmux window and claude process.
- **Inline switcher in last bot message only** — strip reply markup from previous switcher when a new bot message is sent. Never accumulate stale switchers.
- **bypass-only** — claude is launched with `--dangerously-skip-permissions`. No permission relay UI.
- **No message truncation** at parse layer — splitting only at send layer (`split_message`, 4096 char limit). Tables/code that overflow → file attachment.
- **MarkdownV2 via telegramify-markdown** — use `safe_reply`/`safe_edit`/`safe_send` helpers (auto fallback to plain text).
- **Hook-based session tracking** — `SessionStart` hook writes `session_map.json`; monitor polls it to detect session changes.
- **Message queue per user** — FIFO ordering, message merging (3800 char limit), tool_use/tool_result pairing.
- **Rate limiting** — `AIORateLimiter(max_retries=5)` on the Application (30/s global). On restart, the global bucket is pre-filled to avoid burst against Telegram's server-side counter.

## Code Conventions

- Every `.py` file starts with a module-level docstring: purpose clear within 10 lines, one-sentence summary first line, then core responsibilities and key components.
- Telegram interaction: prefer inline keyboards over reply keyboards; use `edit_message_text` for in-place updates; keep callback data under 64 bytes; use `answer_callback_query` for instant feedback.

## Configuration

- Config directory: `~/.ccbot/` by default, override with `CCBOT_DIR` env var.
- `.env` loading priority: local `.env` > config dir `.env`.
- State files: `state.json` (thread bindings), `session_map.json` (hook-generated), `monitor_state.json` (byte offsets).

## Hook Configuration

Auto-install: `ccbot hook --install`

Or manually in `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }]
      }
    ]
  }
}
```

## Architecture Details

See @.claude/rules/architecture.md for full system diagram and module inventory.
See @.claude/rules/dm-architecture.md for DM routing model, active_sessions, switcher, and session lifecycle.
See @.claude/rules/message-handling.md for message queue, merging, and rate limiting.
See @.claude/rules/output-format.md for the rules that shape Claude's reply
formatting when running inside a ccbot session (`CCBOT_INTERFACE=telegram`).
See @.claude/rules/secrets.md for where credentials live (`~/.ccbot/.env`)
and where they must not (CLAUDE.md, any tracked file).
See @doc/dm-multisession-spec.md for the product spec (UX, env vars, acceptance criteria).
See @doc/dm-multisession-plan.md for the implementation plan and hotspot map.

The legacy topic-based routing rule is archived at `doc/legacy/topic-architecture.md` for historical reference only — do not follow it.
