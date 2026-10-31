# ZCode backend contract

The approved scope adds ZCode alongside Claude and Codex, with the same CCBot
session, message, history, attachment, terminal, worker and archive surfaces.
Existing sessions keep their backend. Enabling ZCode never changes another
backend's model, credentials, transcript or active window.

## Native runtime and ownership

CCBot starts the installed `ZCODE_COMMAND` with `ZCODE_FLAGS` (default
`--mode yolo --locale en-US`) in the session's tmux window. Native resume uses
`--resume sess_<uuid>`. Startup must see ZCode's actual composer before queued
input is released. A shell prompt or a quoted composer is not readiness.

ZCode owns its SQLite database. CCBot only opens it with SQLite `mode=ro` and
`query_only`, checks the required schema, and takes consistent read snapshots.
It never migrates, deletes or updates native rows. The documented app-server
`session/read` requires an active runtime owned by that process, so starting a
second runtime to observe the native TUI would give the wrong owner.

CCBot owns an append-only, normalized journal under
`$CCBOT_DIR/zcode-transcripts/`. SessionStart/UserPromptSubmit hooks bind native
session IDs to tmux windows. Temporary hook transcript paths are not retained.
Visible user/assistant text, reasoning, tool calls/results, turn completion,
errors and provider usage are projected into the existing parser's contract.
Hidden model-only inputs never become user messages. Stable source identities
prevent duplicates across polling, process restart and archive restore. A
partially written journal tail is repaired only in this CCBot-owned journal.

## User surfaces

1. ZCode appears in backend settings and the per-node new-session picker.
2. The existing FIFO, cards, tool pairing, background status and input UI work.
3. Stop, kill, archive, native resume and bot restart retain the native ID.
4. History and cross-agent handoff use the normalized journal; the destination
   agent creates its own native session and source history stays intact.
5. File/voice delivery reuses the inbox path; media support follows the model.
6. Model/mode/effort pickers, compact and terminal capture use ZCode's actual UI.
   Models share Codex's direct Telegram option buttons and current-value marker;
   cancellation clears the native command draft.
7. Readiness/auth status respects API-key configuration; provider failures are
   reported from structured errors rather than matching assistant prose.
8. Token/context/cost values come from native usage. Missing account quota or
   reset data is unknown, never zero or an invented Codex 5h/week bucket.
9. Workers advertise ZCode only when installed/configured; their protocol,
   restore, input, history and event paths preserve the backend. This release
   deploys locally; it does not install or restart remote hosts.

## Acceptance

Run the repository CI checks (Ruff, formatting, module budgets, Pyright, pytest)
with focused regressions for schema mismatch/read-only access, partial and
completed tools, hidden inputs, terminal errors, usage/cache accounting,
idempotent projection and restart, backend persistence/pickers, native resume,
worker forwarding and cross-agent history. Also run an isolated real tmux
session through the installed CLI and existing free provider, proving readiness,
input, file tools, turn completion, stop/resume and history projection. Keep
running user sessions, local secrets and other backend defaults intact.
