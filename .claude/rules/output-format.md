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
| "Сводка / pivot / dashboard / summary table" | Inline GFM markdown table — the bot sends it as a Bot API 10.1 rich message and Telegram renders a **native table** (≤ 20 columns). No PNG screenshot needed. |
| Tables that exceed 20 columns or ~50 rows | Even without an explicit "file" request, prefer xlsx — 21+ columns is rejected by the API (the bot falls back to a PNG), and very long tables don't read well on phone anyway. |
| Long fenced code blocks (> 100 lines) | Bot extracts oversized code into a `.py` / `.ts` / etc. attachment automatically; output as you normally would. |
| Mixed prose + small table | Inline markdown — renders natively alongside the prose. |

## Native markdown tables (Bot API 10.1 rich messages)

Write normal GFM tables; the bot's rich-message path renders them
natively on the phone. Rules that matter:

- **≤ 20 columns** (API hard cap; 21+ → the bot diverts to PNG).
- Cells take **inline formatting only**: bold / italic / `code` /
  ~~strike~~ / ==mark== / спойлер / <sup>sup</sup> — no lists, no code
  blocks, no nested tables inside cells.
- Column alignment via GFM separators (`:---`, `:---:`, `---:`) works.
- Keep one table under ~3500 chars so the 4096-per-message split never
  cuts it in half (limit raise is planned).
- Headings (`#`–`######`), `---` rules, task lists, footnotes and
  `<details>` collapsible blocks also render natively — use them
  instead of emoji-pseudo-headers when structure helps.

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

- Prose, code, lists, math — normal markdown output is fine. The bot
  sends it as a rich message (native rendering); if that fails it
  degrades to MarkdownV2 via `telegramify-markdown` automatically.
- Streaming behaviour — these rules apply to the final assistant
  text, not mid-stream tool calls.

## Calibration

Thresholds (`~50 rows`, `~100 lines`) are intentionally soft. Use
judgement: a 12×4 table with terse content reads fine on phone; an
8×80 table doesn't. The bot-side floor is the API itself: ≤ 20
columns renders inline natively (`RICH_TABLE_MAX_COLS`); wider
becomes either a PNG (image-shaped) or a file (data-shaped) depending
on intent. With rich messages disabled (`CCBOT_RICH_MESSAGES=off`)
the legacy MarkdownV2 limits return (`TABLE_MAX_COLS=3`,
`TABLE_MAX_WIDTH=60`).
