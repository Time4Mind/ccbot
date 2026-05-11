# Output formatting for ccbot sessions

This rule shapes how Claude phrases responses **inside a ccbot
Telegram session**. ccbot sets `CCBOT_INTERFACE=telegram` in the
session env so this guidance only applies when the user is reading
through Telegram (not when working in a regular terminal).

Two more env vars are also exported so the session can tell which
deployment it's running under:

| Var | Source | Use |
| --- | --- | --- |
| `CCBOT_HOST` | `CCBOT_HOST` env on the bot host, falling back to `socket.gethostname()` | Identifies the device (e.g. `mac-air`, `arm64-kali`). |
| `CCBOT_BOT_USERNAME` | Telegram `getMe` at bot startup | Identifies the Telegram bot (`@Stefania_tg_bot`). Empty if `getMe` didn't run. |

When the user references "the bot" or "this machine" inside a session,
check these to disambiguate. Don't dump them unsolicited.

## When to give a file vs an inline answer

The Telegram chat surface is narrow, monospace, and lossy on copy-
paste. Users on the desktop terminal don't have that constraint.
Default to phone-friendly output **only when** `CCBOT_INTERFACE`
equals `telegram`.

| User intent | Output |
| ----------- | ------ |
| "Дай файл / save to file / export …" | Real file via `Bash` + Python (`openpyxl` for ≤ 1M rows → `.xlsx`; `pyarrow` / `pandas.to_parquet` for > 1M rows). Print one line: `📎 <relative-path> ready for download`. |
| "Сводка / pivot / dashboard / summary table" | Inline markdown table. The bot will render wide tables as a PNG screenshot — that's what the user sees on phone. |
| Tables that exceed ~10 columns or ~50 rows | Even without an explicit "file" request, prefer xlsx — anything wider/longer doesn't read well on phone, and the user will end up asking for a file anyway. |
| Long fenced code blocks (> 100 lines) | Bot extracts oversized code into a `.py` / `.ts` / etc. attachment automatically; output as you normally would. |
| Mixed prose + small table | Inline markdown is fine — narrow tables (≤ 3 cols, ≤ 60 chars wide) stay inline as text. |

## Writing files for download

Until ccbot ships its `send_file` MCP tool, the bot can't actively
push files to the user. Workflow:

1. Create the file in the current `cwd` (don't scatter into `/tmp`,
   the user can't reach it via Telegram).
2. Use a memorable relative path: `data/forecast.xlsx`,
   `out/users-2026-05-10.parquet`.
3. Print exactly one line in the response so the path is grep-able:
   `📎 data/forecast.xlsx ready` (use the literal `📎` glyph).
4. The user fetches via SCP / git / manual copy. When `send_file`
   ships, the bot will deliver these automatically.

## What stays the same

- Prose, code, lists, math — Claude's normal MarkdownV2-rendered
  output is fine. The bot handles MarkdownV2 conversion via
  `telegramify-markdown`.
- Streaming behaviour — these rules apply to the final assistant
  text, not mid-stream tool calls.

## Calibration

Thresholds (`~10 cols`, `~50 rows`, `~100 lines`) are intentionally
soft. Use judgement: a 12×4 table with terse content reads fine on
phone; an 8×80 table doesn't. The bot's own overflow rule
(`TABLE_MAX_COLS=3`, `TABLE_MAX_WIDTH=60`) is the floor — if your
table fits inline it stays inline; everything wider becomes either
a PNG (image-shaped) or a file (data-shaped) depending on intent.
