"""Application configuration — reads env vars and exposes a singleton.

Loads TELEGRAM_BOT_TOKEN, ALLOWED_USERS, tmux/agent paths, and
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
        # Agent backend. ``claude`` remains the default for backwards
        # compatibility; ``codex`` uses OpenAI Codex CLI and its rollout store.
        self.agent_backend = os.getenv("CCBOT_AGENT_BACKEND", "claude").strip().lower()
        if self.agent_backend not in ("claude", "codex"):
            raise ValueError("CCBOT_AGENT_BACKEND must be 'claude' or 'codex'")
        self.codex_command = os.getenv("CODEX_COMMAND", "codex")
        # Cheap, fast model used for one-shot session auto-naming and
        # ``readable`` picker previews. Keep this separate from the
        # interactive session model.
        self.codex_naming_model = os.getenv(
            "CODEX_NAMING_MODEL", "gpt-5.6-luna"
        ).strip()
        self.codex_flags = os.getenv(
            "CODEX_FLAGS",
            "--dangerously-bypass-approvals-and-sandbox "
            "--dangerously-bypass-hook-trust --enable hooks --no-alt-screen",
        )
        codex_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
        self.codex_sessions_path = Path(
            os.getenv("CCBOT_CODEX_SESSIONS_PATH", str(codex_home / "sessions"))
        )

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

        # Upper bound (seconds) for holding the first message to a freshly
        # `--resume`d window while it auto-compacts. Near-limit transcripts
        # compact for 60-110s on resume; typing into the pane mid-compaction
        # drops the prompt, so the send waits for the pane to settle. On
        # timeout we send anyway (best-effort). 0 disables the gate.
        self.resume_settle_timeout = float(
            os.getenv("CCBOT_RESUME_SETTLE_TIMEOUT", "200")
        )

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

        # --- DM multi-session mode ---
        # Sessions
        self.session_idle_ttl: float = _parse_duration(
            os.getenv("SESSION_IDLE_TTL", "4h"), 4 * 3600
        )
        self.archive_purge_after: float = _parse_duration(
            os.getenv("ARCHIVE_PURGE_AFTER", "14d"), 14 * 86400
        )
        # Background-poll interval for the live /usage modal (used by the
        # quota-crossing alarms in handlers/quota_alerts.py).
        self.quota_alert_poll_interval: float = _parse_duration(
            os.getenv("QUOTA_ALERT_POLL_INTERVAL", "10m"), 10 * 60
        )

        # Preview
        self.preview_user_lines: int = int(os.getenv("PREVIEW_USER_LINES", "4"))
        self.preview_assistant_lines: int = int(
            os.getenv("PREVIEW_ASSISTANT_LINES", "8")
        )
        self.preview_tools: int = int(os.getenv("PREVIEW_TOOLS", "2"))

        # Coalescing window for live card edits — at most one editMessageText
        # per session per CARD_EDIT_LAG seconds. Burst events accumulate into
        # a single edit; the deferred edit always picks up the latest state.
        try:
            self.card_edit_lag: float = float(os.getenv("CARD_EDIT_LAG", "2.0"))
        except ValueError:
            self.card_edit_lag = 2.0

        # Background-session status panel: max badges shown at the end of the
        # active card. Older entries collapse to a "+N more" tail.
        try:
            bg_max = int(os.getenv("BG_STATUS_MAX", "4"))
        except ValueError:
            bg_max = 4
        self.bg_status_max: int = max(1, bg_max)

        # Voice
        voice_backend = os.getenv("VOICE_BACKEND", "auto").strip().lower()
        if voice_backend not in ("auto", "whisper", "apple", "off"):
            voice_backend = "auto"
        self.voice_backend: str = voice_backend
        # q8_0 by default: measured 1.80-1.83x faster than fp16 medium on
        # arm64 (MATMUL_INT8 + i8mm + REPACK put it on the native int8
        # kernel) with byte-identical transcripts on the ru/en samples.
        # Falls back to a pre-existing fp16 ggml-medium.bin so hosts
        # installed before this change keep working — see
        # ``resolve_whisper_model``.
        self.whisper_model_path: str = os.getenv(
            "WHISPER_MODEL_PATH",
            str(self.config_dir / "models" / "ggml-medium-q8_0.bin"),
        )
        # Tiny model used ONLY for the language-detect pre-pass. Whisper
        # re-runs the full encoder when the language is "auto" (12.4 s on
        # medium — half the total cost of a voice message), so detecting
        # on a ~40x cheaper encoder and then pinning -l saves most of it.
        self.whisper_lang_model_path: str = os.getenv(
            "WHISPER_LANG_MODEL_PATH",
            str(self.config_dir / "models" / "ggml-tiny.bin"),
        )
        # Language assumed when detection isn't confident. Russian is the
        # dominant language on this deployment; English is rare, and tiny
        # detects English very reliably (p >= 0.966 on every sample) while
        # its Russian detection is the shaky one — so "default ru, only a
        # confident non-default wins" is the accuracy-preserving shape.
        self.whisper_lang_default: str = (
            os.getenv("WHISPER_LANG_DEFAULT", "ru").strip().lower() or "ru"
        )
        try:
            self.whisper_lang_min_p: float = float(
                os.getenv("WHISPER_LANG_MIN_P", "0.9")
            )
        except ValueError:
            self.whisper_lang_min_p = 0.9
        # whisper-cli defaults to min(4, hw_concurrency); this host has 8
        # cores and the phone still has to stay responsive, so 6 is the
        # compromise (12.1 s vs 14.2 s at 4 and 10.1 s at 8).
        try:
            self.whisper_threads: int = int(os.getenv("WHISPER_THREADS", "6"))
        except ValueError:
            self.whisper_threads = 6
        self.whisper_bin: str = os.getenv("WHISPER_BIN", "whisper-cli")

        # Media inbox
        self.inbox_ttl_hours: float = float(os.getenv("INBOX_TTL_HOURS", "24"))
        self.inbox_dirname: str = os.getenv("CCBOT_INBOX_DIRNAME", ".ccbot-inbox")

        # Claude flags
        self.claude_flags: str = os.getenv(
            "CLAUDE_FLAGS", "--dangerously-skip-permissions"
        )
        self.is_sandbox: bool = os.getenv("IS_SANDBOX", "1") not in ("", "0", "false")

        # Bot API 10.1 rich messages (native markdown rendering). When on,
        # safe_send/safe_reply/safe_edit try sendRichMessage first and fall
        # back to the MarkdownV2 pipeline on any failure. Kill switch:
        # CCBOT_RICH_MESSAGES=off.
        self.rich_messages: bool = os.getenv(
            "CCBOT_RICH_MESSAGES", "on"
        ).strip().lower() not in ("off", "0", "false")

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
