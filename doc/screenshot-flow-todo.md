# Screenshot flow - agreed target and implementation TODO

Status: the original agreed flow was merged in PR #217. The 2026-09-14
follow-up is in local implementation: persistent per-session screenshot cache,
instant cached switching, and background refresh of stale snapshots. It is not
committed or deployed yet.

## Task contract

- Object: terminal screenshots in the Telegram live-session card and the
  composition of the expanded `Options` row.
- Sources: current ccbot base at `3d91411`, current Bria code at `c8d541d`,
  focused screenshot/rich-media tests in both repositories, and the decisions
  recorded below.
- Population: all ccbot users and all active Claude/Codex sessions that have a
  live tmux window.
- Unit of behavior: one user's current active-session card and its existing
  Telegram carrier message.
- Included: option-button visibility, global screenshot state, session-local
  terminal launch, pane capture, PNG rendering/profiles, rich delivery, cache,
  lifecycle cleanup, migration, and removal of superseded screenshot flows.
- Excluded: session rotation, terminal emulator implementation, interactive
  prompt keyboard behavior, card pagination, archive rendering, and unrelated
  menu actions.
- Deliverable: tested local implementation, a local commit, local deployment,
  Telegram acceptance probe, then push/PR/merge only after Artem's explicit OK.
- Writes authorized: local implementation and tests. Commit, deployment and
  publication remain separate release gates.

## Agreed user flow

| State/action | Required result |
|---|---|
| Active card, Options collapsed | Control row contains lifecycle action and `Options`; no screenshot/terminal action row is visible. |
| Tap `Options` | The configured action row opens directly below the control row and directly above session buttons. The main menu does not move or change. |
| Screenshot button configured visible | The row contains `🧑‍💻 Скрин`. Its label has no on/off suffix or state marker. |
| Tap `Скрин` while globally off | Enable screenshots globally for every session and immediately repaint the same active-card carrier with the current pane image. Keep Options expanded. |
| Tap `Скрин` while globally on | Disable screenshots globally and immediately remove the image from the same carrier. Keep Options expanded. |
| Screenshot enabled, session running | Refresh the image only as part of normal active-card updates; do not emit screenshot-only Telegram messages. |
| Screenshot enabled, turn becomes idle | Keep the latest screenshot in the active card. |
| Active session emits a final answer | Freeze the old carrier on its currently open page and remove its buttons. Create the final-answer card as a new message with `✅` before the session icon; the first later card update removes the check. |
| User opens menu/settings/archive/new-session flow or switches away | The former active session becomes background. Its terminal changes must not repaint the visible non-session surface. |
| User returns to/selects an active session | Immediately render that session's own cached snapshot in the same carrier when screenshots are globally enabled. Never carry the source session's image across. |
| Target screenshot cache is at most 10s old | Use it as the completed switch; no synchronous pane capture is needed. |
| Target screenshot cache is older than 10s | Show it immediately, then capture and replace it in the background if the user is still on that session. |
| Target has no screenshot cache | Capture once synchronously so the card never shifts through an image-less intermediate layout. |
| Terminal button configured visible and available | The row contains `🖥 Терминал`; tapping it opens a terminal only for the current active session. It does not enable a global terminal mode. |
| No configured action is currently available | Do not show an empty Options disclosure. |
| Session is closed/archived/deleted | Remove all cached PNG bytes and Telegram file IDs owned by that session. |

The screenshot is placed after the active session's content and before the
`context` line and background-session block:

```text
active-session text
screenshot
context
background sessions
```

## Settings target

### New category: `Кнопки опций`

Render the category as the same native Rich Markdown table pattern used by the
other agreed settings screens:

| Button | Show |
|---|---|
| 🧑‍💻 Скрин | yes/no |
| 🖥 Терминал | yes/no |

Below the table, provide one toggle button per setting. Highlight the selected
state using the existing settings convention.

Defaults for a new user:

- show `Скрин` button: `true`;
- show `Терминал` button: `false`;
- global screenshot state: `false`.

This category controls only which actions can appear in `Options`. Hiding the
`Скрин` button does not implicitly change the global screenshot state.
The action may be restored through Settings and then toggled from `Options`.

### Card category

Remove the old user-facing `Screenshots in card` on/off setting. Replace it
with two image parameters:

| Setting | Values | Default |
|---|---|---|
| Screenshot capture size | `48 KiB`, `64 KiB`, `86 KiB` | `48 KiB` |
| Screenshot quality | `100%, 8 colors`; `75%, 8 colors`; `100%, full palette` | `100%, 8 colors` |

Capture size is a bound on the newest terminal-text suffix, not an image
resolution setting. Profile order is cyclic in the order shown above.

### Terminal category

- Remove the `off / button / always` mode.
- Remove automatic terminal launch for newly created sessions.
- Keep only platform-specific terminal application/template selection where it
  is required (currently Linux).
- Even when configured visible, suppress the action for a session when the
  platform cannot open it or a tmux client is already attached.

## Persistence and migration

Use explicit persisted booleans for option-button visibility. Suggested keys:

- `option_button_screenshot`, default `true`;
- `option_button_terminal`, default `false`.

The exact internal names may change during implementation, but the defaults
and migration behavior are contractual.

- Preserve the existing `card_inline_screenshots` value as the global
  functional screenshot state. It is no longer directly editable in Settings;
  `Опции -> Скрин` owns the toggle.
- If the terminal visibility key is absent, migrate old `local_terminal`:
  `manual` or `auto` -> button visible; `off` or missing -> button hidden.
- Do not preserve automatic launch from old `auto`; after migration it means
  only that the Terminal button remains visible.
- Preserve `local_terminal_cmd` for Linux application/template selection.
- Add capture/profile defaults without changing the user's current global
  screenshot state.
- Decode unknown/old callback data safely; do not revive removed flows.

## Capture and rendering

Retain ccbot's current strengths rather than copying Bria's Go renderer:

- capture the visible active tmux pane with ANSI data;
- retain the existing JetBrains Mono, Noto CJK, and Symbola fallback chain;
- retain 16/256/RGB foreground and background color support;
- keep CPU-heavy rasterization outside the asyncio event-loop thread.

Add the bounded behavior proven in Bria:

- retain a UTF-8-safe suffix within the selected `48/64/86 KiB` budget;
- prefer complete terminal rows after trimming;
- bound rendered rows and columns proportionally to the selected profile;
- cap the rich PNG at 1 MiB and Telegram-supported dimensions;
- if necessary, remove the oldest rows until the newest useful pane content
  fits;
- implement deterministic full-color, full-size eight-color, and 75%
  eight-color profiles.

An invalid/empty capture or render failure must not block the text/keyboard
card update.

## Delivery and cache

- Rich Markdown is the only photo-bearing carrier.
- Upload the first/new PNG in the same send/edit operation as the current card
  text and keyboard.
- Bind a Telegram `file_id` only to the confirmed exact PNG digest returned by
  Telegram.
- Cache identity includes session, captured snapshot, capture limit, and image
  profile.
- Persist the newest confirmed `file_id`, PNG digest, and wall-clock timestamp
  in the session record so restart does not discard the last usable image.
- Reuse the confirmed `file_id` when the exact PNG is unchanged.
- Keep a small bounded per-session cache (Bria uses three images per session).
- Changing capture limit or image profile must prevent stale PNG/file-ID reuse.
- A concurrent render for an obsolete session/profile/state must not overwrite
  the current cache.
- When Rich Media is disabled, unsupported, rejected, or temporarily fails,
  keep/edit the same text-only card. Retry adding the screenshot on a later
  ordinary card update.
- `RetryAfter` remains transport backpressure and must not select another
  carrier type.
- Lost-message recovery remains the existing card-carrier concern; it is not a
  screenshot fallback.

## Remove as superseded

Remove the complete separate screenshot surface:

- `/screenshot` command registration and handler;
- screenshot PNG document messages;
- document Refresh callback;
- document arrow/Enter/Escape/Tab/Space/Ctrl-C keyboard callbacks;
- compact screenshot photo creation;
- compact screenshot session switcher;
- compact screenshot Back/delete/resume flow;
- `CB_SHOT_SW`, `CB_SHOT_BACK`, `CB_SHOT_KEYS`, `CB_SCREENSHOT_REFRESH`, and
  `CB_KEYS_PREFIX` after verifying no non-screenshot consumer remains;
- stale mode parameters, comments, localization strings, exports, and tests for
  the removed paths.

`CB_SHOT_KEYS` is already producer-less in the audited tree: the handler and
constant remain, while the keyboard builder ignores its legacy mode. Remove it
rather than adding a new producer.

Remove the legacy photo+caption live-card carrier and its transitions. On Rich
Media failure, use the agreed text-only fallback. Keep the independent
interactive-prompt keyboard in the live card; it is not part of the removed
screenshot controls.

## Implementation slices

1. Add RED tests for the agreed Options/settings behavior and migration.
2. Introduce option-button visibility settings and remove terminal auto-start.
3. Change `Опции -> Скрин` from separate-photo navigation to an
   immediate global toggle on the same carrier.
4. Add bounded capture options, deterministic profiles, and settings UI.
5. Add exact-PNG cache/file-ID reuse and lifecycle invalidation.
6. Make inline rich delivery persist through idle and degrade only to the same
   text carrier.
7. Remove compact/document screenshot flows and legacy-photo carrier code.
8. Run focused tests after each vertical slice, then the repository full check.

Do not leave compatibility shims that keep the deleted user-visible flows
reachable. Temporary internal shims are acceptable only within one local
implementation branch and must be gone before the candidate commit.

## Acceptance probes

Automated acceptance must cover at least:

- Options row placement and collapse/expand behavior;
- screenshot/terminal visibility defaults and old-settings migration;
- empty Options suppression;
- global screenshot toggle across two sessions;
- terminal action scoped to only the selected active session;
- same `message_id` when screenshot is enabled/disabled/refreshed;
- no screenshot-only notification on a pane-only change;
- screenshot persists after RUNNING -> IDLE;
- no repaint while menu/settings/archive/new-session surfaces are visible;
- image placement before context/background blocks;
- each capture limit and each image profile;
- deterministic profile output, byte/dimension caps, and UTF-8-safe trimming;
- unchanged PNG uses confirmed `file_id` without a multipart re-upload;
- switching never shows another session's screenshot;
- a fresh target cache avoids capture; a stale target cache paints first and
  refreshes asynchronously; a no-cache target captures before its first paint;
- persisted cache survives a state round-trip and bot restart;
- limit/profile changes do not reuse stale media;
- close/archive/delete clears session-owned screenshot cache;
- Rich failure keeps the same text card; `RetryAfter` remains retryable;
- removed commands and callbacks are no longer registered or generated;
- interactive prompt keyboard remains functional.

Local Telegram acceptance must visibly verify:

1. Open Options on an active session and enable Screenshot.
2. Observe the image appear in the same message and in the agreed position.
3. Complete a turn and observe the image remain.
   The final answer must arrive in a new checked card; the previous card must
   remain on its open page without buttons.
4. Open Menu and confirm terminal changes do not overwrite it.
5. Return to the session, switch sessions, and confirm the global state follows
   while each card uses its own current pane.
6. Disable Screenshot and observe same-message removal.
7. Verify Terminal appears only when enabled in Settings and opens only the
   selected session.

## Release gate

Use the established ccbot sequence exactly:

1. Produce and verify one local candidate commit.
2. Deploy that local commit only.
3. Send Artem a separate Bria/Telegram delivery notification that the candidate
   is ready for testing.
4. Wait for Artem's explicit acceptance. If no acceptance arrives within ten
   minutes after the notification/deployment window, roll the local deployment
   back to the prior verified revision.
5. Only after explicit acceptance, push, open/update the PR, pass CI, and merge
   to `main`.
