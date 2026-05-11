"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/Claude paths, and
monitoring intervals from environment variables (with .env support).
.env loading priority: local .env (cwd) > $CCBOT_DIR/.env (default ~/.ccbot).
The module-level `config` instance is imported by nearly every other module.

DM mode adds: SESSION_IDLE_TTL, ARCHIVE_PURGE_AFTER, MAX_SESSIONS,
PREVIEW_*, BG_NOTIFY_MODE, VOICE_BACKEND, WHISPER_MODEL_PATH,
INBOX_TTL_HOURS, QUOTA_ALERT_POLL_INTERVAL.

Key class: Config (singleton instantiated as `config`).
"""

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv

from .utils import ccbot_dir

logger = logging.getLogger(__name__)

# Env vars that must not leak to child processes (e.g. Claude Code via tmux)
SENSITIVE_ENV_VARS = {"TELEGRAM_BOT_TOKEN", "ALLOWED_USERS", "OPENAI_API_KEY"}


def _parse_duration(value: str, default_seconds: float) -> float:
    """Parse a duration string like '4h', '72h', '14d', '60s', '15m' into seconds.

    Bare numbers are treated as seconds. Empty/invalid input returns the default.
    """
    if not value:
        return default_seconds
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", value.lower())
    if not m:
        return default_seconds
    n = float(m.group(1))
    unit = m.group(2)
    multiplier = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
    return n * multiplier


class Config:
    """Application configuration loaded from environment variables."""

    def __init__(self) -> None:
        self.config_dir = ccbot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Load .env: local (cwd) takes priority over config_dir
        # load_dotenv default override=False means first-loaded wins
        local_env = Path(".env")
        global_env = self.config_dir / ".env"
        if local_env.is_file():
            load_dotenv(local_env)
            logger.debug("Loaded env from %s", local_env.resolve())
        if global_env.is_file():
            load_dotenv(global_env)
            logger.debug("Loaded env from %s", global_env)

        self.telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN") or ""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")
        try:
            self.allowed_users: set[int] = {
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            }
        except ValueError as e:
            raise ValueError(
                f"ALLOWED_USERS contains non-numeric value: {e}. "
                "Expected comma-separated Telegram user IDs."
            ) from e

        # Tmux session name and window naming
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "ccbot")
        self.tmux_main_window_name = "__main__"

        # Claude command to run in new windows
        self.claude_command = os.getenv("CLAUDE_COMMAND", "claude")

        # All state files live under config_dir
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Claude Code session monitoring configuration
        # Support custom projects path for Claude variants (e.g., cc-mirror, zai)
        # Priority: CCBOT_CLAUDE_PROJECTS_PATH > CLAUDE_CONFIG_DIR/projects > default
        custom_projects_path = os.getenv("CCBOT_CLAUDE_PROJECTS_PATH")
        claude_config_dir = os.getenv("CLAUDE_CONFIG_DIR")

        if custom_projects_path:
            self.claude_projects_path = Path(custom_projects_path)
        elif claude_config_dir:
            self.claude_projects_path = Path(claude_config_dir) / "projects"
        else:
            self.claude_projects_path = Path.home() / ".claude" / "projects"

        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "2.0"))

        # Display user messages in history and real-time notifications
        # When True, user messages are shown with a 👤 prefix
        self.show_user_messages = (
            os.getenv("CCBOT_SHOW_USER_MESSAGES", "true").lower() != "false"
        )

        # Show tool call notifications (tool_use/tool_result) in Telegram
        # When False, only text responses, thinking, and interactive prompts are sent
        self.show_tool_calls = (
            os.getenv("CCBOT_SHOW_TOOL_CALLS", "true").lower() != "false"
        )

        # Show hidden (dot) directories in directory browser
        self.show_hidden_dirs = (
            os.getenv("CCBOT_SHOW_HIDDEN_DIRS", "").lower() == "true"
        )

        # OpenAI API for voice message transcription (optional)
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_base_url: str = os.getenv(
            "OPENAI_BASE_URL", "https://api.openai.com/v1"
        )

        # --- DM multi-session mode ---
        # Sessions
        self.max_sessions: int = int(os.getenv("MAX_SESSIONS", "10"))
        self.session_idle_ttl: float = _parse_duration(
            os.getenv("SESSION_IDLE_TTL", "4h"), 4 * 3600
        )
        self.archive_purge_after: float = _parse_duration(
            os.getenv("ARCHIVE_PURGE_AFTER", "14d"), 14 * 86400
        )
        # Per-session token alert defaults — three thresholds the user can
        # adjust in Settings. Each must be a positive multiple of 50_000.
        self.session_token_alert_defaults: tuple[int, int, int] = (
            100_000,
            200_000,
            400_000,
        )
        self.session_token_alert_step: int = 50_000

        # Background-poll interval for the live /usage modal (used by the
        # quota-crossing alarms in handlers/quota_alerts.py).
        self.quota_alert_poll_interval: float = _parse_duration(
            os.getenv("QUOTA_ALERT_POLL_INTERVAL", "5m"), 5 * 60
        )

        # Preview
        self.preview_user_lines: int = int(os.getenv("PREVIEW_USER_LINES", "4"))
        self.preview_assistant_lines: int = int(
            os.getenv("PREVIEW_ASSISTANT_LINES", "8")
        )
        self.preview_tools: int = int(os.getenv("PREVIEW_TOOLS", "2"))
        self.preview_live_lag: float = float(os.getenv("PREVIEW_LIVE_LAG", "4"))

        # Coalescing window for live card edits — at most one editMessageText
        # per session per CARD_EDIT_LAG seconds. Burst events accumulate into
        # a single edit; the deferred edit always picks up the latest state.
        try:
            self.card_edit_lag: float = float(os.getenv("CARD_EDIT_LAG", "2.0"))
        except ValueError:
            self.card_edit_lag = 2.0

        # How many of the most-recent tool-call lines to keep visible in the
        # session card. Older tool calls collapse to a single "… N earlier
        # tool calls collapsed" placeholder. Recommended range 3..7.
        try:
            visible = int(os.getenv("CARD_VISIBLE_TOOLS", "3"))
        except ValueError:
            visible = 3
        self.card_visible_tools: int = max(1, visible)

        # Notifications
        bg_mode = os.getenv("BG_NOTIFY_MODE", "separate").strip().lower()
        if bg_mode not in ("separate", "footer"):
            bg_mode = "separate"
        self.bg_notify_mode: str = bg_mode

        # Voice
        voice_backend = os.getenv("VOICE_BACKEND", "auto").strip().lower()
        if voice_backend not in ("auto", "whisper", "apple", "off"):
            voice_backend = "auto"
        self.voice_backend: str = voice_backend
        self.whisper_model_path: str = os.getenv(
            "WHISPER_MODEL_PATH",
            str(self.config_dir / "models" / "ggml-medium.bin"),
        )
        self.whisper_bin: str = os.getenv("WHISPER_BIN", "whisper-cli")

        # Media inbox
        self.inbox_ttl_hours: float = float(os.getenv("INBOX_TTL_HOURS", "24"))
        self.inbox_dirname: str = os.getenv("CCBOT_INBOX_DIRNAME", ".ccbot-inbox")

        # Claude flags
        self.claude_flags: str = os.getenv(
            "CLAUDE_FLAGS", "--dangerously-skip-permissions"
        )
        self.is_sandbox: bool = os.getenv("IS_SANDBOX", "1") not in ("", "0", "false")

        # Optional outbound proxy for the Telegram Bot API. Useful when the
        # host is on a network that cannot reach api.telegram.org directly
        # (e.g. RU-blocked IPs). Accepts http://host:port or socks5://host:port.
        self.tg_proxy_url: str = os.getenv("TG_PROXY_URL", "").strip()

        # Identifying label for this deployment — surfaced to Claude via
        # ``CCBOT_HOST`` so a session can tell which device it's running
        # on (Mac vs. arm64 box etc.). Defaults to ``socket.gethostname()``
        # so the env stays meaningful out of the box; override in .env
        # when the hostname is opaque.
        import socket

        self.host_label: str = (
            os.getenv("CCBOT_HOST", "").strip() or socket.gethostname()
        )

        # Filled at runtime in ``bot.app.post_init`` from
        # ``Application.bot.username`` so we can surface ``@<botname>`` to
        # Claude via ``CCBOT_BOT_USERNAME``. Empty until that runs — code
        # that uses it must tolerate the empty case.
        self.bot_username: str = ""

        # Scrub sensitive vars from os.environ so child processes never inherit them.
        # Values are already captured in Config attributes above.
        for var in SENSITIVE_ENV_VARS:
            os.environ.pop(var, None)

        logger.debug(
            "Config initialized: dir=%s, token=%s..., allowed_users=%d, "
            "tmux_session=%s, claude_projects_path=%s",
            self.config_dir,
            self.telegram_bot_token[:8],
            len(self.allowed_users),
            self.tmux_session_name,
            self.claude_projects_path,
        )

    def is_user_allowed(self, user_id: int) -> bool:
        """Check if a user is in the allowed list."""
        return user_id in self.allowed_users


config = Config()
