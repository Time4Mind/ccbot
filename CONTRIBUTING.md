# Contributing to ccbot

Thanks for your interest in ccbot. This is a personal-use Telegram bot
for driving Claude Code sessions from a phone — small, opinionated, and
single-user by design. Patches that align with that scope are welcome.

## Before you start

ccbot is a **fork** that diverges from the upstream
[`Time4Mind/ccbot`](https://github.com/Time4Mind/ccbot) in two large ways:

1. **DM-only / single-user.** No supergroup, no forum topics, no
   multi-user routing. The only chat the bot ever sees is a private 1-1
   DM with one allowlisted Telegram user id.
2. **Bypass-only.** Claude is launched with
   `--dangerously-skip-permissions`. There is no permission relay UI.

If your change reintroduces topics, multi-user routing, or a permission
prompt path, it's almost certainly out of scope — please open an issue
first to discuss before sending a PR.

The full design rationale lives in `doc/dm-multisession-spec.md`.

## Local setup

```bash
uv sync
cp .env.example .env  # fill in TELEGRAM_BOT_TOKEN + ALLOWED_USERS
ccbot hook --install  # one-time: registers Claude Code SessionStart hook
ccbot                 # runs in foreground
```

Tests don't need a real bot token; they use stubbed handlers.

## Quality bar

Every PR must pass these locally **and** in CI:

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/   # auto-fix; verify with --check
uv run pyright src/ccbot/
uv run pytest
```

Pre-commit hooks enforce additional invariants — install with:

```bash
uv tool install pre-commit
pre-commit install
```

The `forbid-personal-markers` and `forbid-corporate-author` hooks block
real names, IPs, hostnames, internal email domains, and absolute
`/Users/*` paths from leaking into the public tree. If a hook fires,
fix the offending content — never bypass with `--no-verify`.

## Code conventions

- **Module docstrings.** Every `.py` file starts with a module-level
  docstring: one-sentence summary on the first line, then a few lines
  on responsibilities and public API.
- **Structured logging on hot paths.** Prefer
  `logger.info("event_name", extra={"key": value, ...})` over
  `logger.info(f"...")` in inbound/outbound message routers, queue
  workers, and other repeated events. The first argument is a stable
  `snake_case` event name; everything contextual goes in `extra=`.
  This is what the optional `CCBOT_LOG_FORMAT=json` mode uses to
  produce one JSON line per record. One-shot logs from rare paths can
  stay as plain strings.
- **600-LOC ceiling** under `src/ccbot/bot/`. Files in `src/ccbot/` may
  go up to 800 LOC temporarily; over that, decompose in the same PR.
- **No comments explaining "what"** — the code says that. Only write
  comments when the *why* is non-obvious (a hidden constraint, a
  workaround, surprising behaviour).
- **Inline keyboards over reply keyboards** for Telegram UI. Keep
  callback data ≤ 64 bytes; ack with `answer_callback_query`.
- **No truncation at parse layer.** Long content splits at the send
  layer (`split_message`, 4096-char cap) — tables/code that overflow
  attach as files.
- **MarkdownV2 only via** `safe_reply` / `safe_edit` / `safe_send`.
  Never call `parse_mode=MARKDOWN_V2` directly — those helpers fall
  back to plain text on parse failure.

## Commits and PRs

- Conventional commit prefixes: `feat:`, `fix:`, `chore:`, `docs:`,
  `test:`, `refactor:`.
- One PR, one purpose. Don't bundle a refactor with a feature with a
  bugfix.
- The PR description should state which modules changed, what risks
  exist, and how you tested it. For UI changes, include screenshots
  from the Telegram client.
- CI must be green before merge. No "we'll fix it after."
- Force-push to shared branches (`main`, `feat/*`) is `--force-with-lease`
  only and only after notifying the maintainer.

## Cherry-picking from upstream

When backporting an upstream commit, name it with the source SHA so the
trail stays auditable:

```
cherry-pick: <upstream-sha> — <subject>
```

`doc/upstream-drops.md` records upstream commits we explicitly skipped
along with the reason (typically: incompatible with DM-only or
bypass-only invariants).

## What not to do

- Don't add new runtime dependencies without a clear reason in the PR.
- Don't introduce a new secret-storage mechanism (Vault, KMS, etc.).
- Don't add cloud SaaS integrations (Sentry, Datadog, LogRocket).
- Don't change the Claude auth model — `claude.ai` login (Max) is the
  only supported path.
- Don't reintroduce topic-based routing or multi-user features.
- Don't translate the README to Russian — the public docs stay
  English; localised notes belong in `doc/ru/`.

## Questions

Open an issue. The maintainer reads everything; small clarification
questions are fine before you start coding.
