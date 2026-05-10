"""Slash-command handlers — one Telegram command per function.

Sub-modules:
  - lifecycle: session lifecycle commands (/new, /list, /use, /rename,
    /kill, /done, /stop, /menu, /archive).
  - info:     read-only info commands (/status, /history, /screenshot,
    /usage).

Each command is a top-level ``async def *_command(update, context)``
suitable for direct registration via ``CommandHandler``.
"""
