"""TG formatting calibration — table / long-code overflow → file attachment.

Spec section 12 (O1): keep the existing `telegramify-markdown` rendering
pipeline for small content; when a code block exceeds size thresholds, peel
it out into a downloadable file with a short inline preview. Markdown tables
that cannot render legibly in TG (more than 3 columns or >60-char width) are
likewise emitted as `.md` attachments.

Public API:
  split_overflow(text) -> FormatResult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Thresholds — calibrated empirically; overridable via env in a future commit.
CODE_MAX_LINES = 120
CODE_MAX_CHARS = 3000
CODE_PREVIEW_LINES = 30
TABLE_MAX_COLS = 3
TABLE_MAX_WIDTH = 60


@dataclass
class Attachment:
    filename: str
    content: bytes


@dataclass
class FormatResult:
    text: str
    attachments: list[Attachment] = field(default_factory=list)


_FENCE_RE = re.compile(r"^(```+)([^\n`]*)\n(.*?)(?<=\n)\1[ \t]*$", re.DOTALL | re.MULTILINE)


def _ext_for_lang(lang: str) -> str:
    lang = (lang or "").strip().lower()
    return {
        "py": "py",
        "python": "py",
        "ts": "ts",
        "typescript": "ts",
        "tsx": "tsx",
        "js": "js",
        "javascript": "js",
        "jsx": "jsx",
        "go": "go",
        "rs": "rs",
        "rust": "rs",
        "json": "json",
        "yaml": "yaml",
        "yml": "yaml",
        "toml": "toml",
        "sh": "sh",
        "bash": "sh",
        "zsh": "sh",
        "sql": "sql",
        "md": "md",
        "html": "html",
        "css": "css",
        "tsv": "tsv",
        "csv": "csv",
    }.get(lang, "txt")


def _make_inline_preview(code: str, lang: str) -> str:
    """Take the first CODE_PREVIEW_LINES lines as the inline preview block."""
    lines = code.splitlines()
    preview = "\n".join(lines[:CODE_PREVIEW_LINES])
    fence_lang = lang or ""
    return f"```{fence_lang}\n{preview}\n... ({len(lines) - CODE_PREVIEW_LINES} more lines in attached file)\n```"


def _table_rows(text: str) -> list[tuple[int, int, str]]:
    """Find consecutive markdown-table line ranges. Returns (start, end, slice)."""
    out: list[tuple[int, int, str]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and "|" in lines[i][1:]:
            j = i
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if j - i >= 2:
                out.append((i, j, "\n".join(lines[i:j])))
            i = j
            continue
        i += 1
    return out


def _table_cols(table_text: str) -> int:
    first = table_text.splitlines()[0].strip().strip("|")
    return len([c for c in first.split("|") if c.strip() != ""]) or 1


def _table_width(table_text: str) -> int:
    return max((len(line) for line in table_text.splitlines()), default=0)


def split_overflow(text: str) -> FormatResult:
    """Pull long fenced code and wide tables into attachments; keep small ones inline."""
    if not text:
        return FormatResult(text=text)

    out_text = text
    attachments: list[Attachment] = []

    # 1) Code fences.
    def _replace_code(match: re.Match[str]) -> str:
        lang = match.group(2).strip()
        body = match.group(3)
        line_count = body.count("\n")
        if line_count <= CODE_MAX_LINES and len(body) <= CODE_MAX_CHARS:
            return match.group(0)
        idx = len(attachments) + 1
        ext = _ext_for_lang(lang)
        attachments.append(
            Attachment(filename=f"code-{idx}.{ext}", content=body.encode("utf-8"))
        )
        return _make_inline_preview(body, lang) + f"\n_(see code-{idx}.{ext})_"

    new_text = _FENCE_RE.sub(_replace_code, out_text)
    if new_text != out_text:
        out_text = new_text

    # 2) Markdown tables.
    tables = _table_rows(out_text)
    if tables:
        lines = out_text.splitlines()
        # Walk in reverse so indices stay valid.
        for start, end, slab in reversed(tables):
            cols = _table_cols(slab)
            width = _table_width(slab)
            if cols <= TABLE_MAX_COLS and width <= TABLE_MAX_WIDTH:
                continue
            idx = len(attachments) + 1
            attachments.append(
                Attachment(
                    filename=f"table-{idx}.md",
                    content=(slab + "\n").encode("utf-8"),
                )
            )
            replacement = (
                f"_(table {cols}×{end - start} attached as table-{idx}.md)_"
            )
            lines = lines[:start] + [replacement] + lines[end:]
        out_text = "\n".join(lines)

    return FormatResult(text=out_text, attachments=attachments)
