"""Slash-command handlers — one Telegram command per function.

Sub-modules:
  - lifecycle: session lifecycle commands (/new, /kill, /stop,
    /menu, /archive).
  - info:     read-only info commands (/usage, /health, /help).

Each command is a top-level ``async def *_command(update, context)``
suitable for direct registration via ``CommandHandler``.
"""
