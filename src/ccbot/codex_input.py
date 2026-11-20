"""Guard local Codex prompt delivery at the point of terminal input."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from .terminal_parser import is_interactive_ui, strip_codex_identity_footer

COMPOSER_TIMEOUT = 60.0
_PROMPT = re.compile(r"^\s*[›>](?:\s|$)")
_MENU_OPTION = re.compile(r"^\s*›\s*\d+[.)]\s+")


def composer(pane: str) -> str | None:
    """Read the live editor, ignoring approval text in completed turns."""
    body = strip_codex_identity_footer(pane)
    lines = body.rstrip().splitlines()
    for i in range(len(lines) - 1, -1, -1):
        if not _PROMPT.match(lines[i]):
            continue
        tail = "\n".join(lines[i:])
        if (_MENU_OPTION.match(lines[i]) and body == pane) or is_interactive_ui(tail):
            return None
        if re.search(
            r"(?im)^\s*(?:press enter to confirm|enter to submit\s*\|\s*esc)", tail
        ):
            return None
        if any(
            re.search(r"^\s*(?:[•·]\s*)?Working\b|esc to interrupt", line, re.I)
            for line in lines[i + 1 :]
        ):
            return None
        return tail.lstrip()[1:].strip()
    return None


def matches_draft(draft: str, pasted: str, *, allow_folded: bool) -> bool:
    # tmux wraps even inside words. Preserve punctuation and character order.
    visible = "".join(draft.split())
    expected = "".join(pasted.split())
    if visible == expected and expected:
        return True
    # A tall paste scrolls the start of the live editor above the viewport.
    if len(visible) >= 80 and expected.endswith(visible):
        return True
    if allow_folded and re.fullmatch(r"\[Image #\d+\]", draft):
        from .input_recovery import input_recovery

        request = input_recovery.current()
        if request and request.media_path:
            path = Path(request.media_path)
            return pasted.strip() in {str(path), f".ccbot-inbox/{path.name}"}
    if allow_folded and re.fullmatch(r"\[Pasted Content (\d+) chars\]", draft):
        return draft in {
            f"[Pasted Content {n} chars]" for n in (len(pasted), len(pasted.rstrip()))
        }
    return False


class ComposerGuard:
    def __init__(
        self, window: str, capture: Callable[[str], Awaitable[str | None]]
    ) -> None:
        self.window = window
        self.capture = capture
        self.deadline = time.monotonic() + COMPOSER_TIMEOUT

    async def ready(self, pasted: str = "", *, submitting: bool = False) -> bool:
        """After an interruption, resume only with the original draft intact."""
        from .tmux_input_transport import codex_process_is_running

        interrupted = False
        redraw_deadline = None
        while True:
            if not await codex_process_is_running(self.window):
                return False
            pane = await self.capture(self.window)
            if pane is None:
                return False
            draft = composer(pane)
            if draft is not None:
                if not pasted or not (submitting or interrupted):
                    return True
                # The screen can repaint after tmux acknowledges paste-buffer.
                # Never press Enter on an empty or unrelated composer.
                if matches_draft(draft, pasted, allow_folded=not interrupted):
                    return True
                if redraw_deadline is None:
                    redraw_deadline = time.monotonic() + 1.0
                if time.monotonic() >= redraw_deadline:
                    return False
                await asyncio.sleep(0.05)
                continue
            if not pasted:
                from .input_recovery import input_recovery
                from .input_composer_queue import ComposerBusy

                request = input_recovery.current()
                if request and request.strict_codex_proof:
                    raise ComposerBusy()
            interrupted = True
            if time.monotonic() >= self.deadline:
                return False
            await asyncio.sleep(0.25)
