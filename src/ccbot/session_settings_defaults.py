"""Default per-user settings for the DM session pool."""

from typing import Any

from .session_defaults import DEFAULT_IDLE_ARCHIVE_HOURS


DEFAULT_USER_SETTINGS: dict[str, Any] = {
    "language": "en",  # "en" | "ru" | "zh" - UI strings
    "live_lag": 4,  # seconds, see PREVIEW_LIVE_LAG
    "voice": "auto",  # "auto" | "parakeet" | "whisper" | "apple" | "off"
    # Optional conservative rewrite before a prompt reaches the pinned
    # session. It is deliberately opt-in: migrations and new users both
    # remain on the direct-delivery path until they choose a mode.
    "preprocessing_mode": "off",  # "off" | "voice" | "all"
    # Empty selects the approved built-in instruction. A custom value
    # replaces it verbatim; it is never mixed into the main agent context.
    "preprocessing_instruction": "",
    # Hours without activity before a live session is archived. 6h is the
    # closest supported migration from the historical global 4h default.
    "session_idle_hours": DEFAULT_IDLE_ARCHIVE_HOURS,
    # Day-of-week the Anthropic weekly window resets on. Drives the %/d
    # burn-rate computation in the Menu quota table. Values: "mon".."sun".
    "weekly_reset_day": "mon",
    # Auto-approve interactive Yes/No prompts that --dangerously-skip-
    # permissions doesn't already bypass (e.g. WebFetch per-domain
    # trust). "off" = surface in TG, "on" = auto-Yes on every prompt.
    "auto_approve": "off",
    # Three states for the desktop terminal companion:
    #   off    - never spawn, never offer
    #   manual - don't auto-spawn, but show "Open terminal" in Menu
    #            when the active session has no attached tmux client
    #   auto   - auto-spawn on session create AND show the manual
    #            button whenever no client is attached
    # On Linux ``manual``/``auto`` also need ``local_terminal_cmd``
    # (or CCBOT_LOCAL_TERMINAL_CMD env) - without an emulator template
    # the button is hidden because the click would silently no-op.
    # Legacy binary "on" is auto-migrated to "auto" on read.
    "local_terminal": "off",
    # Linux: command template used by ``local_terminal``. Empty means
    # "fall back to CCBOT_LOCAL_TERMINAL_CMD or skip". Templates are
    # picked from a known list in Settings -> Local terminal, or set
    # manually via env. Use ``{shell}`` as the placeholder for the
    # shell-quoted attach snippet.
    "local_terminal_cmd": "",
    # Disposition of the user's outgoing text relative to the live
    # How many trailing end_turn boundaries to pull from the JSONL
    # transcript when seeding an empty live-card state (e.g. after
    # a bot restart, after switcher-tap / Menu -> Sessions on a fresh
    # state). Higher = more in-card scrollback at the cost of memory
    # (each turn ~= several events x ~500 bytes).
    "card_history": 20,
    # Global screenshot state. The Options action toggles it and the
    # active Rich Markdown card transforms in place; text is the fallback.
    "card_inline_screenshots": False,
    # Which actions are disclosed by the live-card Options button.
    # Visibility is independent from the global screenshot state above.
    "option_button_screenshot": True,
    "option_button_terminal": False,
    "option_button_transfer": False,
    # Pane suffix budget and deterministic image profile.
    "screenshot_capture_kib": 48,
    "screenshot_profile": "full8",
    # Bg session push notifications (Task #42). Three independent
    # toggles - user asked to make each granular. Default all-on
    # so the user knows what bg sessions are doing.
    "bg_notify_finished": True,
    "bg_notify_error": True,
    "bg_notify_needs_action": True,
    # Max page size in logical \n-delimited LINES. Values 30/50/70/100.
    # 30 keeps the card compact on phone; 100 is for power users who
    # scroll long bodies. Anchor (page top) chunking handles overflow
    # with smart sentence / paragraph boundaries - see
    # ``_chunk_final_text`` for the exact preference order.
    "card_page_lines": 30,
    # Legacy shared spoiler limit retained only as a migration source.
    "spoiler_block_lines": 10,
    # Independent visible rows for the command and result blocks.
    "spoiler_command_lines": 10,
    "spoiler_result_lines": 10,
    # Auto-rename new sessions via a cheap one-shot model call after the
    # first user message >=20 chars. When ``False``, names stay as
    # the directory basename (``workdir``, ``workdir-2``, ...) for
    # the session's lifetime. The persisted key keeps its historical
    # name for state-file compatibility.
    "haiku_naming": True,
    # Summarise the first two user requests in Archive with the same
    # isolated cheap model used for session naming. Off shows both prompts.
    "archive_ai_description": False,
    # Keep one empty agent session prewarmed for the selected directory.
    "default_session_enabled": False,
    "default_session_directory": "",
    "default_session_backend": "",
    # Empty means the historical bot-wide backend only (read migration).
    "enabled_backends": [],
}
