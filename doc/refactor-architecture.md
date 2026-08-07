# Refactored module map

This tree keeps the existing product behaviour and import paths, but separates
implementation by reason to change. Historical modules such as
`ccbot.session`, `ccbot.bot.messages`, `ccbot.handlers.card_model`, and
`ccbot.handlers.notifications` remain compatibility facades.

## Where a change belongs

| Change | Primary module |
|---|---|
| Add or edit UI copy | `i18n_locales/en.py`, `ru.py`, `zh.py` |
| Add a setting and its choices | `handlers/menu_settings_data.py` |
| Change a settings keyboard | `handlers/menu_settings.py` |
| Change footer/menu composition | `handlers/menu.py` |
| Change persisted session state | `session_state.py` |
| Change hook/window bindings | `session_map.py` |
| Change resume readiness or prompt delivery | `session.py` |
| Change tmux process cleanup | `tmux_process.py` |
| Change tmux window creation or backend command | `tmux_window.py` |
| Change terminal interactive/status parsing | `terminal_parser.py` |
| Change terminal `/usage` parsing | `terminal_usage.py` |
| Change transcript DTOs | `transcript_types.py` |
| Change Claude/Codex transcript normalization | `transcript_message.py`, `transcript_codex.py` |
| Change Telegram app startup/shutdown | `bot/_app_lifecycle.py` |
| Register a Telegram handler | `bot/_app_routes.py` |
| Change text routing | `bot/_messages_text.py` |
| Change voice routing | `bot/_messages_voice.py` |
| Change photo/document/forward handling | `bot/_messages_media.py` |
| Change shared inbound delivery rules | `bot/_messages_shared.py` |
| Add card state or an event field | `handlers/card_types.py` |
| Parse monitor output into a card event | `handlers/card_events.py` |
| Sanitize transcript text for cards | `handlers/card_text.py` |
| Change one event's visual form | `handlers/card_event_render.py` |
| Change line/byte budgets | `handlers/card_budget.py` |
| Change page boundaries or page selection | `handlers/card_pagination.py` |
| Change the complete card layout | `handlers/card_layout.py` |
| Change card ownership and lock state | `handlers/card_registry.py` |
| Seed a card from JSONL | `handlers/card_seed.py` |
| Move/pause/restore a card carrier | `handlers/card_carrier.py` |
| Send or edit a Telegram card | `handlers/card_transport.py` |
| Change RUNNING-only inline-pane placement or rich photo reuse | `handlers/card_rich_media.py` |
| Apply/finalize session events | `handlers/card_updates.py` |
| Keep silent turns observable / mark bg stalls | `handlers/card_stall.py` |
| Change card timer/panel scheduling | `handlers/card_surface.py` |
| Change archived-session history rendering | `handlers/history_archive.py` |
| Change live history cache/presentation | `handlers/history.py` |
| Change auto-approval parsing | `handlers/status_approval.py` |
| Change status polling orchestration | `handlers/status_polling.py` |
| Change archive list blurbs | `handlers/archive_blurb.py` |
| Change archive restore/sweep flow | `handlers/archive.py` |

## Dependency direction

Keep dependencies pointing from orchestration toward leaf modules:

```text
types/data -> parsing and pure formatting -> state services
           -> Telegram/tmux adapters -> lifecycle/composition roots
```

- Data and parsing modules must not import Telegram application assembly.
- `card_types.py` contains state only; it must not acquire I/O or persistence.
- Telegram transport belongs in `card_transport.py`, not rendering modules.
- Compatibility facades may synchronize monkeypatchable dependencies, but new
  business logic belongs in the focused implementation module.
- Avoid generic `helpers.py` modules. Name a module after the responsibility
  that will cause it to change.

## Compatibility policy

Old import paths remain stable until a separate breaking migration. Some
underscore-prefixed symbols are imported or monkeypatched by the current test
suite and therefore form a de-facto compatibility contract. When moving a
stateful function, preserve mutable object identity and ensure patches on the
facade still reach the implementation.

## Size guard

`python scripts/check_module_size.py` enforces the hard limits:

- `src/ccbot/bot/**/*.py`: 600 physical lines;
- all other `src/ccbot/**/*.py`: 800 physical lines.

Aim below 550 and 700 respectively so the next feature has room. A module near
the hard cap should be split around a responsibility boundary in the same
change that grows it.
