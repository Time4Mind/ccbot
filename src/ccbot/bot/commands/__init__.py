"""Slash-command handlers — one Telegram command per function.

Sub-modules:
  - lifecycle: session lifecycle commands (/new, /kill, /done, /stop,
    /menu, /archive).
  - info:     read-only info commands (/history, /screenshot,
    /usage, /health, /help).

Each command is a top-level ``async def *_command(update, context)``
suitable for direct registration via ``CommandHandler``.
"""
