"""Recognize the live Codex slow-response overlay, never quoted scrollback."""

import re
from dataclasses import dataclass

TITLE = "Giving this request a little extra thought"
_INTRO = (
    "If you'd rather not wait, retry with a faster model. It may be less capable "
    "of handling complex requests."
)
_FOOTER = (
    "No action is required. Codex will keep waiting, and this menu will close "
    "when the response is ready."
)
_CHOICES = (
    "Retry with a faster model",
    "Dismiss and keep waiting",
    "Learn more",
)


@dataclass(frozen=True)
class CodexWaitPrompt:
    selected: int
    content: str


def parse_codex_wait_prompt(pane: str) -> CodexWaitPrompt | None:
    """Require the entire current modal, including its final footer.

    A prompt/composer below an old copy invalidates the match. Incomplete
    redraws also fail closed, since Enter must never choose the faster model.
    """
    lines = [line.strip() for line in pane.splitlines()[-40:] if line.strip()]
    starts = [i for i, line in enumerate(lines) if line == TITLE]
    if not starts:
        return None
    lines = lines[starts[-1] :]
    option_start = next(
        (i for i, line in enumerate(lines) if re.fullmatch(r"[›❯]?\s*1\. .+", line)),
        None,
    )
    if option_start is None or " ".join(lines[1:option_start]) != _INTRO:
        return None
    options = lines[option_start : option_start + 3]
    if len(options) != 3:
        return None
    selected = []
    for number, (line, label) in enumerate(zip(options, _CHOICES), 1):
        match = re.fullmatch(rf"([›❯]?)\s*{number}\. {re.escape(label)}", line)
        if match is None:
            return None
        if match[1]:
            selected.append(number)
    if len(selected) != 1:
        return None
    if " ".join(lines[option_start + 3 :]) != _FOOTER:
        return None
    return CodexWaitPrompt(selected[0], "\n".join(lines))
