# Security policy

## Scope

ccbot is a personal-use Telegram bot that bridges a private DM to a
single user's Claude Code sessions running in tmux. It is **not** a
multi-tenant service. The intended deployment model is: one user, one
host, one allowlisted Telegram user id.

Even in that single-user model, several attack-surface concerns are
worth taking seriously:

- The bot launches `claude --dangerously-skip-permissions` — anyone
  who can deliver a Telegram message that the bot accepts effectively
  has shell access on the host.
- Telegram bot tokens grant full impersonation of the bot until
  revoked.
- Voice transcription, when configured to use OpenAI, sends user
  audio to a third party.
- The hook integration writes to `~/.claude/settings.json`.

If you find a vulnerability that could be abused under those
assumptions — privilege escalation, sandbox escape, token exfiltration,
unauthorized session takeover — please report it privately rather than
filing a public issue.

## How to report

Preferred channel: **GitHub Security Advisories** on this repo.
"Report a vulnerability" creates a private discussion with the
maintainer, and lets us coordinate a fix and disclosure window.

If you can't use Security Advisories for some reason, email the
maintainer (see `git log` for the committer address) with a subject
that begins with `[security]`.

Please include:

- A short description of the issue and the kind of attacker it
  empowers (local user, network attacker, allowlisted Telegram user
  with a malicious link, …).
- Reproduction steps or a minimal proof-of-concept.
- Affected commit / branch.
- Whether the issue has been disclosed elsewhere already.

We aim to respond within 7 days.

## Out of scope

The following do not constitute vulnerabilities for our threat model:

- The host running ccbot can be fully controlled by anyone in
  `ALLOWED_USERS` — that is the intended behaviour.
- Telegram messages are stored on Telegram's servers — nothing the
  bot does can change that.
- The OpenAI fallback voice backend transmits audio to OpenAI when
  the user explicitly enables it.
- Anything that requires already having root access on the host.

## Defensive practices we already follow

- Sensitive env vars (`TELEGRAM_BOT_TOKEN`, `ALLOWED_USERS`,
  `OPENAI_API_KEY`) are scrubbed from `os.environ` after read so they
  don't leak to child processes (`tmux`-spawned `claude`).
- `.gitleaks.toml` plus a pre-commit gitleaks hook block secret
  patterns from being committed. The CI `secrets-scan` workflow
  catches anything that slips past local hooks.
- `pre-commit` regex hooks (`forbid-personal-markers`,
  `forbid-corporate-author`) block real names, internal IPs/hostnames,
  corporate emails, and absolute `/Users/*` paths from public files.
- All inbound user messages are gated by `is_user_allowed(user_id)` —
  unrecognised senders are dropped silently.

## Disclosure

After a fix lands, we'll publish a GitHub Security Advisory describing
the issue, affected versions, and the remediation. Reporters are
credited unless they request anonymity.
