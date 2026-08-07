---
name: ccbot-send-file
description: Deliver a file produced or selected by Claude Code or Codex to the current user's Telegram chat through the repository's built-in `ccbot send-file` relay. Use when an agent needs to attach an image, document, archive, report, or other local file to the CCBot conversation, verify outbound delivery, or troubleshoot target-chat resolution without exposing credentials.
---

# Send a file through CCBot

Use the built-in relay first. Do not ask for the bot token during normal
operation. Keep direct Telegram delivery as the reserve path.

## Deliver

1. Resolve the exact local file and verify that it is a regular file.
2. Run:

   ```bash
   ccbot send-file "/absolute/path/to/file" --caption "Short description"
   ```

   Omit `--caption` when it adds no value. If `ccbot` is not on `PATH`, use the
   repository environment, for example `.venv/bin/ccbot send-file ...` or
   `uv run ccbot send-file ...`.
3. Treat exit code `0` and the emitted `sent ...: ok` line as delivery proof.
   Report a non-zero exit and its sanitized error; do not claim success.

`ccbot send-file` automatically switches from the filesystem relay to direct
Telegram delivery when the daemon relay is unavailable. Let that built-in
fallback finish before trying anything else.

Image extensions `.png`, `.jpg`, `.jpeg`, `.webp`, and `.gif` are sent as
Telegram photos. Other files are sent as documents with their filename.

## Resolve the target safely

Target precedence is:

1. Explicit `--chat-id ID` only when the user requested a specific allowed chat.
2. `CCBOT_CHAT_ID`, injected automatically into an owned Claude/Codex tmux
   session. This is the normal path.
3. Every ID from `ALLOWED_USERS` when the session has no single owner.

Do not print IDs unless troubleshooting requires identifying the target and the
user authorized it. The daemon rejects IDs outside `ALLOWED_USERS`.

## Locate configuration without exposing it

Configuration lookup order is repository `.env`, then
`${CCBOT_DIR:-~/.ccbot}/.env`. Relevant variable names are:

- `CCBOT_CHAT_ID`: current session target; normally present in the process
  environment, not stored manually.
- `ALLOWED_USERS`: allowed numeric Telegram user IDs.
- `TELEGRAM_BOT_TOKEN`: daemon credential originally obtained from BotFather.

The running daemon already owns `TELEGRAM_BOT_TOKEN`; `ccbot send-file` normally
uses its filesystem relay and does not need the agent to read the token. Check
only whether a variable or config file exists. Never echo, log, paste, commit,
or include token/ID values in a command transcript, caption, filename, or answer.

## Reserve direct channel

Use a manual direct call only when the `ccbot send-file` entry point itself
cannot run, not merely while it is waiting for its relay result. Prefer the
project implementation over handwritten `curl`: load `ccbot.config.config`,
resolve the same target precedence with `ccbot.send_file.resolve_chat_ids`, and
call `ccbot.send_file._send_all(path, caption, chat_ids)` from the repository's
Python environment. Pass the path and caption as arguments or constants; never
embed token or chat-ID values in the script or command line.

The direct channel still reads `TELEGRAM_BOT_TOKEN` and `ALLOWED_USERS` from the
normal configuration lookup. If configuration is absent, stop and report which
variable name is missing. Do not request or reveal its value in chat.

## Guardrails

- Send only the file the user requested or a clearly identified task artifact.
- Inspect filenames and intended contents for credentials, private keys, `.env`
  data, access tokens, cookies, personal data, and unrelated workspace content.
- Ask before sending when the file's sensitivity or target is ambiguous.
- Prefer an absolute, explicitly quoted path; do not use broad globs.
- Do not switch to the direct channel or retry blindly after a timeout:
  Telegram may have accepted the first delivery. Check the command result and
  bot logs first.
- Keep generated artifacts in the task/repository scope; do not copy secrets
  into a new file merely to send them.
