# Upstream drift — classification

Companion to `doc/dm-multisession-spec.md` and the `B5` rule in
`ccbot-tz-followup.md`. Every commit on `upstream/main` ahead of
`feat/dm-multisession` lands in one of four buckets:

- **cherry-pick** — bugfix or feature that is compatible with the
  DM-only / single-user / bypass-only invariants. Backport with
  `cherry-pick: <upstream-sha> — <subject>` in the commit message.
- **adapt** — concept fits the fork after non-trivial rework. Open a
  separate PR documenting what changed.
- **already-applied** — semantics already present (often via different
  code paths). No action needed; record so we don't re-evaluate it
  next sweep.
- **drop** — incompatible with the fork's invariants. Recording the
  reason here keeps the rationale auditable and prevents the same
  question from coming back at the next sweep.

Sweep cadence: monthly, per `B5` of `ccbot-tz-followup.md`.

Last classified: 2026-05-10. Range:
`git log feat/dm-multisession..upstream/main` (86 commits).

---

## Drop — topics / supergroup routing

Touch the topic-routing surface that we deleted in Phase 1 (`bot.py`,
`session.py`). Reintroducing them would re-create exactly the
multi-user / forum / thread-binding mess the fork removed.

| Upstream | Subject | Reason |
| -------- | ------- | ------ |
| `350c653` | fix: stop renaming user-created Telegram topics on bind | topic-bind path deleted |
| `539bd3b` | feat: sync Telegram topic name to tmux window on rename | topic API gone |
| `db1de84` | fix: restore /kill alongside /unbind | `/unbind` is topic-only; we kept `/kill` |
| `11ddde1` | feat: add /unbind to disconnect topic without killing session | topic API gone |
| `6efbc1f` | docs: restore /kill mention | docs for topic flow |
| `3c2d4f9` | fix: use group chat_id instead of user_id for supergroup forum | supergroup-only |
| `e83379a` | feat: window picker for unbound topics + auto-rename | topic-bind path |
| `ded5408` | Fix multi-topic routing bugs | topics |
| `a585990` | fix: support multiple supergroups per user | supergroup-only |
| `5afc111` | Fix forum topic message routing | supergroup-only |
| `44222ee` | fix: group_chat_id regression tests, docs | supergroup-only |
| `51cc5b6` | Merge PR #23 — fix/group-chat-id-routing | supergroup-only |
| `26cb81f` | refactor: remove group_chat_ids, use user_id directly | already done by Phase 1 |
| `8395ef7` | docs: update READMEs to reflect window_id-keyed routing | upstream README, ours rewritten |

## Drop — formatter / converter divergence

Upstream toggled HTML mode on, then off. We chose MarkdownV2 +
`telegramify-markdown` and stayed there.

| Upstream | Subject | Reason |
| -------- | ------- | ------ |
| `1d37a2d` | feat: add HTML formatter option via chatgpt_md_converter | HTML path not used |
| `ef79072` | refactor: migrate to chatgpt-md-converter (HTML), remove MarkdownV2 | reverted upstream too |
| `20e3794` | fix: revert to telegramify-markdown, remove chatgpt-md-converter | already on telegramify |
| `1d01da4` | fix: check HTML length after conversion in split_message | HTML path |
| `23e79c8` | fix: add missing convert_markdown in edit_message_text paths | HTML path |
| `7ce59a6` | refactor: move markdown conversion from response_builder to send layer | we already do this |
| `5bafeef` | feat: render markdown tables as card-style KV pairs | adapt-candidate, not drop — see below |

## Drop — Claude Code SDK / process-tree paths

The fork relies on the SessionStart hook + tmux as the source of
truth, not the WebSocket SDK or process detection.

| Upstream | Subject | Reason |
| -------- | ------- | ------ |
| `71cc989` | docs: reverse-engineered Claude Code WebSocket SDK protocol | SDK not used |
| `b3c7de9` | Remove Claude Code exit detection and restart button | exit detection not used |
| `23dc27b` | Detect Claude Code exit and notify user with restart button | superseded by reconcile/lost |
| `7786646` | refactor: re-key internal routing from window_name to window_id | already done |
| `547a47a` | refactor: move pane parsing functions into terminal_parser.py | already done |

## Drop — already-have / refactoring noise

Already present in our tree (often via different code paths) or pure
upstream meta-commits with no code value to us.

| Upstream | Subject | Reason |
| -------- | ------- | ------ |
| `865ab89` | Fix interactive UI creating duplicate messages on button press | verify, but our interactive_ui is reworked |
| `a85321f` | feat: voice transcription via OpenAI API | we have whisper.cpp + Apple Speech |
| `2b99b8c` | fix: use find_window_by_id instead of find_window_by_name | already done |
| `1408bce` | fix: improve rate limiting with AIORateLimiter(max_retries=5) | already done |
| `fb5b3b3` | refactor: replace manual rate limiting with AIORateLimiter | already done |
| `a563abd` | fix: clean up old-format session_map keys at startup | covered by `resolve_stale_ids` |
| `4fec542` | ci: add ruff to dev dependencies | already have |
| `566fa95` | Add CI workflow: formatter, lint, and tests | we have our own CI |
| `0e3c31c` | Add test suite (177 tests) and pytest config | we have ours |
| `1088e03` | docs: restructure CLAUDE.md | our CLAUDE.md is bespoke |
| `87d570f` | docs: add contributors section to README | upstream README |
| `45dc771` | docs: simplify .env setup instructions | upstream README |
| `af265ed` | feat: local .env takes priority | already done |
| `e0fbcf9` | feat: friendly config error message and non-source install docs | already friendly |
| `602b57a` | feat: configurable config directory via CCBOT_DIR | already done |
| `73dc1fd` | Support custom CLAUDE_CONFIG_DIR for Claude variants | already done (`CCBOT_CLAUDE_PROJECTS_PATH`) |
| `52c09d8` | fix: prevent bot token from leaking to Claude subprocess | covered by `SENSITIVE_ENV_VARS` |
| `30fd83a` | style: fix ruff formatting | nothing to backport |
| `970abca` | refactor: remove dead code detected by vulture | superseded by Phase 1 / A1 refactor |
| `7a2a2c6` | fix: add github_token to claude code review workflow | upstream-only workflow |
| `12502a1` | fix: use pull_request_target for claude code review workflow | upstream-only workflow |
| `4ebdfb8` | Add demo video | upstream README artifact |
| `b2fa2dd` | Update demo video | upstream README artifact |
| `e55ccbf` | Delete PLAN.md | upstream housekeeping |
| `a288d75` | Add MIT license | already have |
| `1376d10` | Add Chinese README documentation | we have `README_CN.md` |
| `0b47465` | sync: align with Mage212 main on top of upstream/main | upstream merge meta |
| `101824e` | Revert "fix: strip ANSI escape codes from transcript text output" | superseded by `70183a0` |
| `9587189` | fix: strip ANSI escape codes from transcript text output | superseded by `70183a0` |
| `8452440` | Add Settings UI detection and Space/Tab keyboard buttons | already have |
| `62e02ef` | Add control keys keyboard to screenshot, increase UI refresh delay | already have |
| `4a2ae82` | fix: preserve message order for interactive UI detection | already have |
| `ad23d8b` | fix: send interactive UI after tool_use message in queue worker | already have |
| `593a4d1` | fix: improve interactive UI detection for permission prompts | already have via `INTERACTIVE_TOOL_NAMES` |
| `ab564c4` | Fix AskUserQuestion parsing for multi-tab UI | already have |
| `2cdbaad` | Fix AskUserQuestion not detecting ☒ (selected tab) | verify |
| `fb63bc3` | Add Edit tool permission prompt pattern to interactive UI detection | covered by `INTERACTIVE_TOOL_NAMES` |
| `0b9e287` | feat: add session resume picker when creating new topics | DM dir-browser is richer |
| `85b9315` | feat: add /usage command | we have it (and dedicated ccbot-usage window) |
| `61e3d0b` | feat: capture and display ! bash command output in topic | already have |
| `04185ab` | feat: support ! command mode in send_keys | already have |
| `3aaa733` | feat: add CCBOT_SHOW_HIDDEN_DIRS option | already have |
| `10cf776` | feat: add /model command support | already covered by command-forwarding |
| `55d54be` | fix: remove extraneous f-string prefixes in main.py | nothing to backport |

## Cherry-pick candidates

Backwards-compatible bugfixes worth verifying we don't already have,
then backporting with a `cherry-pick: <sha> — <subject>` commit.

| Upstream | Subject | Verification target |
| -------- | ------- | ------------------- |
| `f5ddd7f` | fix: show correct line count for Write tool results | `transcript_parser` Write summary |
| `4e7bf99` | feat: add CCBOT_SHOW_TOOL_CALLS and CCBOT_SHOW_USER_MESSAGES | we read both today; commit may extend semantics |
| `69bb86d` | telegramify-markdown >= 0.5.0, < 1.0.0 pin | check `pyproject.toml` |
| `aebc7a9` | fix: convert markdown tables before splitting | `tg_format.py` overflow path |
| `72cf5b6` | fix: handle hook timeout when resuming sessions | `_session_create.py` resume path |
| `c769cc0` | fix: recover from corrupted byte offset in JSONL reading | `monitor_state.py` |
| `3178d75` | fix: clean up stale session_map entries on startup | `session_manager.load_session_map` |
| `70183a0` | fix: strip ANSI escape codes from parsed message text | `transcript_parser` text path |
| `dc75228` | fix: file size check to prevent delayed message delivery | `session_monitor` |
| `84dd4c7` | Fix Korean (Hangul) characters rendered with wrong font in screenshots | `screenshot.py` font fallback |
| `65f4bbe` | fix: clear status message when user sends new input | `handlers/message_queue.py` |
| `fbc6112` | fix: send interactive UI as plain text instead of markdown | `handlers/interactive_ui.py` |
| `49bf869` | fix: anchor parse_status_line on chrome separator | `terminal_parser.parse_status_line` |
| `0bd53d4` | Fix JSONL partial line read causing missed messages | `session_monitor` byte-offset logic |
| `af6fc14` | fix: prevent Claude Code from overriding tmux window names | `tmux_manager` rename guard |

## Adapt candidates

Concept fits but the fork's surface differs enough that a direct
cherry-pick won't apply. Open a dedicated PR.

| Upstream | Subject | Adaptation note |
| -------- | ------- | --------------- |
| `4f1a703` | feat: forward base64 images between Telegram and Claude Code | Already partly covered by `inbox.py`; could surface inline previews. |
| `5bafeef` | feat: render markdown tables as card-style KV pairs | Could replace `tg_format.py` table-overflow → file path with inline KV when narrow. Visual decision. |

---

## Process

When backporting:

1. `git cherry-pick -x <sha>` (the `-x` flag adds the upstream
   reference to the message body automatically).
2. Adjust the subject line to start with `cherry-pick:` per
   `CONTRIBUTING.md`.
3. If the patch needs adaptation for the DM model, mark it as such
   in the commit body and reference the relevant rule in
   `.claude/rules/dm-architecture.md`.
4. Move the row from "cherry-pick candidates" / "adapt candidates" to
   a new section "Already cherry-picked" with the local commit SHA
   captured.

Run the next sweep with:

```bash
git fetch upstream main
git log feat/dm-multisession..upstream/main --oneline
```

Anything new since the last sweep needs classification.
