"""Message splitting utility for Telegram's 4096-character limit.

Provides:
  - split_message(): splits long text into Telegram-safe chunks (≤4096 chars),
    preferring newline boundaries and preserving code block integrity.
"""

TELEGRAM_MAX_MESSAGE_LENGTH = 4096


def _is_table_separator(stripped: str) -> bool:
    """``|---|:--:|`` style GFM separator row (already stripped)."""
    core = stripped.strip("|").replace("|", "").replace(" ", "")
    return bool(core) and set(core) <= {"-", ":"}


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks that fit Telegram's length limit.

    Tries to split on newlines when possible to preserve formatting.
    When a split occurs inside a fenced code block (```), the block is
    closed at the end of the current chunk and re-opened at the start
    of the next chunk so each chunk remains valid markdown. Likewise,
    when a split occurs inside a GFM table body, the header + separator
    rows are re-emitted at the start of the next chunk so every chunk
    renders as a valid table.
    """
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    in_code_block = False
    code_fence = ""  # e.g. "```python"
    table_header = ""
    table_sep = ""
    in_table_body = False

    for line in text.split("\n"):
        stripped = line.strip()

        # Track code block state
        if stripped.startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_fence = stripped  # remember "```lang"
            else:
                in_code_block = False

        # Track table state (a `|` line inside a code fence is not a table)
        if not in_code_block and stripped.startswith("|"):
            if not table_header:
                table_header = line
                table_sep = ""
                in_table_body = False
            elif not table_sep:
                if _is_table_separator(stripped):
                    table_sep = line
                else:
                    # Second row isn't a separator — not a GFM table;
                    # treat the current line as a fresh candidate header.
                    table_header = line
            else:
                in_table_body = True
        else:
            table_header = ""
            table_sep = ""
            in_table_body = False

        # If single line exceeds max, split it forcefully
        if len(line) > max_length:
            if current_chunk:
                chunk_text = current_chunk.rstrip("\n")
                if in_code_block:
                    # The long line is inside a code block; close before flush
                    chunk_text += "\n```"
                chunks.append(chunk_text)
                current_chunk = (code_fence + "\n") if in_code_block else ""
            # Split long line into fixed-size pieces
            for i in range(0, len(line), max_length):
                chunks.append(line[i : i + max_length])
        elif len(current_chunk) + len(line) + 1 > max_length:
            # Current chunk is full, start a new one
            chunk_text = current_chunk.rstrip("\n")
            if in_code_block:
                chunk_text += "\n```"
            chunks.append(chunk_text)
            # Re-open code block / re-emit table header in the new chunk
            if in_code_block:
                current_chunk = code_fence + "\n" + line + "\n"
            elif in_table_body:
                prefix = table_header + "\n" + table_sep + "\n"
                if len(prefix) + len(line) + 1 <= max_length:
                    current_chunk = prefix + line + "\n"
                else:
                    current_chunk = line + "\n"
            else:
                current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks
