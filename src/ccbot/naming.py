"""H6 — auto-generated session names via a one-shot Haiku call.

`generate_name(seed_text)` shells out to ``claude --model haiku --no-resume``
with a fixed prompt and returns a kebab-case name on success, or None on any
failure (network, missing CLI, malformed output). Cost: tiny — ~50 tokens of
input/output, single turn, charged to the user's Max x20 subscription.

Triggered at most once per session by `maybe_auto_name`, which inspects the
Session record and the seed message and decides whether to fire.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex

from .config import config
from .session import session_manager

logger = logging.getLogger(__name__)


_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


_PROMPT_TEMPLATE = (
    "Generate a 2-3 word kebab-case name (lowercase, hyphenated) for a "
    "coding session about: {seed}\nReply with only the name, no quotes."
)


async def _run(cmd: str, *, timeout: float = 30.0) -> str | None:
    """Run a shell command, return stdout on success or None on any failure."""
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
    if _NAME_RE.match(first_line):
        return first_line
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
    cmd = (
        f"{shlex.quote(claude_bin)} --model haiku --no-resume "
        f"--print {shlex.quote(prompt)}"
    )
    raw = await _run(cmd, timeout=30.0)
    if raw is None:
        return None
    name = _sanitize(raw)
    if not name:
        logger.debug("naming: rejected raw output: %r", raw[:80])
        return None
    return name


async def maybe_auto_name(session_id: str, seed_text: str) -> None:
    """Trigger an auto-name attempt if appropriate. Fire-and-forget safe.

    Conditions:
      - Session.name is empty or starts with "session-".
      - Session not already auto-named (state.json idempotency).
    """
    sess = session_manager.get_session(session_id)
    if sess is None:
        return
    name = sess.name or ""
    looks_default = (not name) or name.startswith("session-")
    if not looks_default:
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
