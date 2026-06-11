"""TG formatting calibration — table / long-code overflow → file attachment.

Spec section 12 (O1): keep the existing `telegramify-markdown` rendering
pipeline for small content; when a code block exceeds size thresholds, peel
it out into a downloadable file with a short inline preview.

Tables: with rich messages on (`config.rich_messages`, Bot API 10.1) GFM
tables render natively, so only tables beyond the API's 20-column cap are
diverted to a PNG attachment. With rich off, the legacy MarkdownV2 limits
apply (more than 3 columns or >60-char width → PNG).

Public API:
  split_overflow(text) -> FormatResult
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import config

# Thresholds — calibrated empirically; overridable via env in a future commit.
CODE_MAX_LINES = 120
CODE_MAX_CHARS = 3000
CODE_PREVIEW_LINES = 30
TABLE_MAX_COLS = 3
TABLE_MAX_WIDTH = 60
# Bot API 10.1 hard cap — 21+ columns is rejected with
# RICH_MESSAGE_TABLE_COLS_TOO_MANY, so such tables still go out as PNG.
RICH_TABLE_MAX_COLS = 20


@dataclass
class Attachment:
    filename: str
    content: bytes
    # ``"document"`` (default) → ``bot.send_document``
    # ``"photo"`` → ``bot.send_photo`` (PNG screenshot of a wide table)
    kind: str = "document"


@dataclass
class FormatResult:
    text: str
    attachments: list[Attachment] = field(default_factory=list)


_FENCE_RE = re.compile(
    r"^(```+)([^\n`]*)\n(.*?)(?<=\n)\1[ \t]*$", re.DOTALL | re.MULTILINE
)


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


def pretty_pad_table(md_text: str) -> str:
    """Re-pad markdown-table cells so each column reaches a uniform width.

    LLMs frequently emit raw `| a | very long | b |` rows where the
    column edges drift line-to-line. Monospace-render output looks
    ragged. This helper parses the table, computes max width per
    column, and pads cells to that width — separator row gets a
    consistent run of dashes too.

    Non-table input passes through untouched.
    """
    lines = md_text.splitlines()
    rows: list[list[str]] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            return md_text
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        rows.append(cells)
    if len(rows) < 2:
        return md_text
    cols = max(len(r) for r in rows)
    rows = [r + [""] * (cols - len(r)) for r in rows]
    widths = [max(len(r[c]) for r in rows) for c in range(cols)]
    out: list[str] = []
    for i, r in enumerate(rows):
        # Separator row (second line, mostly dashes) — replace with a clean
        # run of `-` matching column widths.
        if i == 1 and all(set(c) <= {"-", ":"} for c in r):
            out.append("|" + "|".join("-" * (widths[c] + 2) for c in range(cols)) + "|")
            continue
        out.append("| " + " | ".join(r[c].ljust(widths[c]) for c in range(cols)) + " |")
    return "\n".join(out)


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

    # 2) Markdown tables → PNG attachments. Wide tables don't render well
    # inline on phones, but a monospaced screenshot does. Caller in
    # ``notifications._send_attachments`` rasterises ``kind="photo"``
    # entries via ``screenshot.text_to_image`` just before sending.
    tables = _table_rows(out_text)
    if tables:
        lines = out_text.splitlines()
        for start, end, slab in reversed(tables):
            cols = _table_cols(slab)
            width = _table_width(slab)
            if config.rich_messages:
                # Native rich-table rendering — divert only what the API
                # itself rejects (> RICH_TABLE_MAX_COLS columns).
                if cols <= RICH_TABLE_MAX_COLS:
                    continue
            elif cols <= TABLE_MAX_COLS and width <= TABLE_MAX_WIDTH:
                continue
            idx = len(attachments) + 1
            # Store the source markdown — sender will pretty-pad and
            # render to PNG. Use UTF-8 bytes so the Attachment.content
            # field stays byte-typed across kinds.
            attachments.append(
                Attachment(
                    filename=f"table-{idx}.png",
                    content=(slab + "\n").encode("utf-8"),
                    kind="photo",
                )
            )
            replacement = f"_(table {cols}×{end - start} attached as table-{idx}.png)_"
            lines = lines[:start] + [replacement] + lines[end:]
        out_text = "\n".join(lines)

    return FormatResult(text=out_text, attachments=attachments)
