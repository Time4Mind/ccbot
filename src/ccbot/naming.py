"""H6 — auto-generated session names via a one-shot Haiku call.

``generate_name(seed_text)`` shells out to ``claude --model haiku --print``
with a fixed prompt and returns a kebab-case name on success, or None on
any failure (network, missing CLI, malformed output). Cost: tiny — ~50
tokens of input/output, single turn, charged to the user's Max x20
subscription.

``--print`` makes the call non-interactive and one-shot, so it never
attempts to resume a prior session — no need for the old ``--no-resume``
flag (which was removed from the upstream CLI and started failing every
invocation with ``unknown option '--no-resume'``).

Triggered at most once per session by ``maybe_auto_name``, which
inspects the Session record and the seed message and decides whether to
fire.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from .config import config
from .session import session_manager

logger = logging.getLogger(__name__)


_DIR_SUFFIX_RE = re.compile(r"-\d+$")


def _looks_default_name(name: str, workdir: str) -> bool:
    """True when ``name`` still matches the directory-basename pattern
    used at session creation (``basename`` or ``basename-N``).

    The trigger guard in ``maybe_auto_name`` was historically
    ``name.startswith("session-")``, which never matched real sessions
    (the code never produces a ``session-N`` placeholder — tmux always
    gives the window the cwd's basename). So Haiku auto-naming silently
    never fired. Pinning the guard to the actual default pattern keeps
    manually-renamed sessions (``/rename``, ``/new <name>``) intact
    while letting Haiku take over the cwd-derived ones.
    """
    if not name:
        return True
    if not workdir:
        return False
    base = Path(workdir).name
    if not base:
        return False
    if name == base:
        return True
    # ``basename-N`` collision suffix from ``tmux_manager.create_window``.
    if name.startswith(f"{base}-") and _DIR_SUFFIX_RE.search(name[len(base) :]):
        return True
    return False


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


_PROMPT_TEMPLATE = (
    "Generate a 2 word kebab-case name (lowercase, hyphenated, exactly two "
    "words) for a coding session about: {seed}\nReply with only the name, "
    "no quotes."
)


# Refusal openers Haiku sometimes returns instead of a name (most often
# when the seed mentions a real person, sensitive context, or otherwise
# trips a safety reflex). Sanitization happily reduces "I cannot help
# with that." → "i-cannot" → "i cannot", which passes the kebab-case
# regex and ends up as the session's display name. Reject these at the
# raw-output layer so naming falls back to the directory basename.
_REFUSAL_PREFIXES = (
    "i cannot",
    "i can't",
    "i can not",
    "i won't",
    "i will not",
    "i'm unable",
    "i am unable",
    "i'm not able",
    "i am not able",
    "i'm sorry",
    "i am sorry",
    "i apologize",
    "i refuse",
    "sorry",
    "unfortunately",
    "as an ai",
    "as a language model",
)


def _looks_like_refusal(raw: str) -> bool:
    """True when Haiku's reply opens with a known refusal phrase."""
    if not raw:
        return False
    head = raw.strip().lower()
    return any(head.startswith(p) for p in _REFUSAL_PREFIXES)


# Hard cap on auto-name word count (hyphen-separated tokens). The prompt
# already asks Haiku for two words, but models drift — this enforces it.
_MAX_NAME_WORDS = 2


# Env vars that mark "we're being invoked from inside a Claude Code
# session". Inheriting them into the naming subprocess makes the new
# ``claude`` invocation try to resume / nest under the parent's session
# id, which causes spurious failures (wrong cwd, stale tools, etc.).
# Scrub them so each Haiku call is a clean one-shot.
_CLAUDE_SESSION_ENV_KEYS = (
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDECODE",
    "AI_AGENT",
)


def _build_naming_env() -> dict[str, str]:
    """Subprocess env for ``claude --model haiku --print``.

    Two reasons we don't just inherit ``os.environ``:

    1. ``~/.claude/settings.json`` sets ``permissions.defaultMode =
       bypassPermissions``, which is equivalent to passing
       ``--dangerously-skip-permissions``. Under root, the CLI rejects
       that combo unless ``IS_SANDBOX=1`` is set — so we force it.
    2. Inherited ``CLAUDE_CODE_SESSION_ID`` / ``CLAUDECODE`` etc. make
       the child think it's nested inside a Claude Code session and
       trip up its lifecycle hooks (the parent's SessionStart hook
       writes ``session_map.json`` against the WRONG session id).
    """
    env = {k: v for k, v in os.environ.items() if k not in _CLAUDE_SESSION_ENV_KEYS}
    env["IS_SANDBOX"] = "1"
    return env


async def _run(
    *argv: str, timeout: float = 30.0, env: dict[str, str] | None = None
) -> str | None:
    """Run a command via ``exec``, return stdout on success or None on any failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as e:
        logger.debug("naming: subprocess spawn failed: %s", e)
        return None
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        logger.debug("naming: subprocess timed out")
        return None
    if proc.returncode != 0:
        logger.debug(
            "naming: claude exit %s: %s",
            proc.returncode,
            stderr.decode(errors="replace")[:200],
        )
        return None
    return stdout.decode(errors="replace").strip()


def _sanitize(text: str) -> str:
    """Best-effort cleanup of Haiku output to a kebab-case identifier."""
    text = text.strip().strip("'\"`")
    # Take the first word/line if model produced extra prose.
    first_line = text.splitlines()[0] if text else ""
    first_line = first_line.lower()
    first_line = re.sub(r"[^a-z0-9-]+", "-", first_line)
    first_line = re.sub(r"-+", "-", first_line).strip("-")
    # Cap to the first N hyphen-separated words (default 2).
    if first_line:
        first_line = "-".join(first_line.split("-")[:_MAX_NAME_WORDS])
    if _NAME_RE.match(first_line):
        # Display names use spaces; the regex validates the kebab form.
        return first_line.replace("-", " ")
    return ""


async def generate_name(seed_text: str) -> str | None:
    """Run a one-shot Haiku call to produce a session name.

    `seed_text` is the user's message that triggered naming. Trimmed to ~200
    chars to keep token cost negligible.
    """
    seed = (seed_text or "").strip().replace("\n", " ")[:200]
    if not seed:
        return None
    claude_bin = config.claude_command or "claude"
    prompt = _PROMPT_TEMPLATE.format(seed=seed)
    raw = await _run(
        claude_bin,
        "--model",
        "haiku",
        "--print",
        prompt,
        timeout=30.0,
        env=_build_naming_env(),
    )
    if raw is None:
        return None
    if _looks_like_refusal(raw):
        logger.debug("naming: rejected refusal output: %r", raw[:80])
        return None
    name = _sanitize(raw)
    if not name:
        logger.debug("naming: rejected raw output: %r", raw[:80])
        return None
    return name


async def maybe_auto_name(
    session_id: str, seed_text: str, user_id: int | None = None
) -> None:
    """Trigger an auto-name attempt if appropriate. Fire-and-forget safe.

    Conditions:
      - User's ``haiku_naming`` setting is ON (when ``user_id`` is given).
      - Session.name still matches the directory-basename pattern
        (``basename`` or ``basename-N``) — never overwrites a manual
        rename.
      - Session not already auto-named this session (re-entrancy guard).
    """
    sess = session_manager.get_session(session_id)
    if sess is None:
        return

    if user_id is not None:
        settings = session_manager.get_user_settings(user_id)
        if not settings.get("haiku_naming", True):
            return

    if not _looks_default_name(sess.name or "", sess.workdir or ""):
        return

    # Use a cheap inline marker on the Session to avoid double-firing.
    flag = "_auto_named"
    if getattr(sess, flag, False):
        return
    setattr(sess, flag, True)

    new_name = await generate_name(seed_text)
    if not new_name:
        # Reset flag so a future trigger can retry.
        setattr(sess, flag, False)
        return
    if new_name == sess.name:
        return
    session_manager.rename_session(sess.id, new_name)
    logger.info("Auto-named session %s -> %s", sess.id, new_name)
