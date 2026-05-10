"""ccbot.bot — Telegram bot package, entry-point + handler graph.

Exports:
  - ``create_bot()``        — build the Application; called by ``ccbot.main``.
  - ``forward_command_handler`` — re-exported because the test suite (and
     historically the legacy bot.py) reaches for it directly.

Internal layout:
  ``app.py``              bootstrap, lifecycle hooks, handler registration.
  ``_common.py``          tiny helpers shared across every sub-module.
  ``_usage_window.py``    dedicated tmux window for /usage queries.
  ``messages.py``         text / voice / photo / document handlers,
                          forward_command_handler, ! bash capture,
                          create_and_activate_session.
  ``session_events.py``   handle_new_message — claude → TG dispatch.
  ``commands/``           slash-command handlers grouped by intent.
  ``callbacks/``          inline-keyboard callback handlers split by
                          ``callback_data`` prefix.
"""

from .app import create_bot
from .messages import forward_command_handler

__all__ = ["create_bot", "forward_command_handler"]
