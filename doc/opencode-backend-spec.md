# OpenCode backend

OpenCode is a separate backend alongside Claude, Codex and ZCode. Enabling it
does not change existing sessions or another backend's credentials or model.
This adapter targets OpenCode 1.x and validates the native SQLite schema before
reading it.

Install the official OpenCode CLI, then connect its own OpenAI provider using
`opencode auth login --provider openai` and select ChatGPT Pro/Plus. CCBot does
not copy credentials from Codex or configure API billing. A recorded OAuth login
enables the readiness probe; only a completed inference confirms model access.

Install the lifecycle plugin using `ccbot hook --install --backend opencode`.
It writes only `ccbot.js` under OpenCode's global `plugins` directory. It publishes
session-created and user-message bindings only inside CCBot-owned tmux sessions.
An existing TUI must be restarted to load a newly installed plugin.

Configuration:

| Variable | Default | Purpose |
|---|---|---|
| `OPENCODE_COMMAND` | `opencode` | CLI executable |
| `OPENCODE_FLAGS` | empty | Additional native TUI flags |
| `OPENCODE_MODEL` | `openai/gpt-6-luna` | Model passed as `--model` by CCBot and used for automatic session names |
| `OPENCODE_SESSION_DB_PATH` | `$XDG_DATA_HOME/opencode/opencode.db` | Optional native database override. When `XDG_DATA_HOME` is unset, use `~/.local/share` |

Set `model` in OpenCode's own configuration for launches outside CCBot. Within
CCBot, changing the current model through `/model` affects that session. It
leaves the configured startup model in place for new launches. The native
`/new` command in an already running OpenCode 1.19 TUI carries over any model
selected in that terminal.

Workers also accept `CCBOT_OPENCODE_COMMAND`. They advertise OpenCode only when
its executable and OpenAI OAuth configuration are present. Worker installation
and authentication are local to each node.

CCBot launches the native TUI in its own window and uses `--session ses_...` for
native resume. Input is gated on the native composer and a running OpenCode
process. `/model` maps to `/models`, `/clear` maps to `/new`. The local model picker
uses named Telegram buttons, like Codex, for the complete available native
catalogue (`opencode models --verbose` in the session directory/configuration).
Recent models and the terminal viewport do not limit the catalogue. Large lists
use named page buttons. A model is selected through its native search dialog;
CCBot checks the exact filtered choice and verifies the applied model before
confirming it, including when the effort dialog covers the composer. Supported native
variants then appear as named mode buttons, including Default. Default is never
selected on the user's behalf. A cancelled mode picker retains the model already
selected, as with Codex's model-then-effort flow. Expired, cross-user and repeated
model buttons cannot select a mode or submit a prompt. Queued input waits until
selection finishes, including the short transition between model and mode.
The catalogue is read on the owning local node; a remote session without catalogue
support displays an error and Cancel instead of a leader's unrelated models.
The CCBot lifecycle plugin disables question, plan-enter and plan-exit tools
for built-in and configured agents. It converts configured ask permissions and
native ask defaults to deny while retaining explicit allow/deny rules. Such
turns return normal text instead of requesting keyboard/menu responses. This
policy is scoped to CCBot OpenCode processes; other backends are unchanged. One Stop action sends OpenCode's required double Escape while a turn
is running; dismissing a dialog sends a single Escape.

OpenCode does not retain prompts submitted while its TUI is busy. Literal
requests therefore wait in a per-window FIFO until the native composer is
ready, then require a new matching user row in the native store before delivery
is acknowledged. Waiting never holds the navigation lock, so Stop stays usable.
An uncertain submission is not retyped automatically.

OpenCode owns its database. CCBot uses SQLite `mode=ro`, `query_only` and a
consistent transaction; it never migrates or updates native tables. Visible
messages, reasoning, paired tools, terminal errors and usage are projected to
an append-only CCBot journal under `$CCBOT_DIR/opencode-transcripts/`. Synthetic
and model-only inputs are hidden. Stable identities prevent replay duplicates;
an interrupted append repairs only the CCBot journal. Histories, archive restore,
cross-backend handoff and worker events retain native backend identity.

Tool cards use the shared command/result spoiler renderer. The adapter retains
complete arguments before display limits, uses native file text and patch diffs,
preserves image attachments and unknown MCP tools, and maps native tool names
to the existing display names. Nonzero shell exit codes appear as tool errors;
an explicit user abort closes pending tools as Interrupted rather than a
provider failure. Native output truncation stays visible.

Recorded token usage comes from native completed messages. When the local
OpenCode OAuth account matches the verified local Codex account, the menu
shows current Codex account limits under `OpenCode · Codex`. The quota is the
shared account quota. If the accounts cannot be matched or the limits cannot
be read, the menu can still show OpenCode's recorded tokens.

Release acceptance requires repository checks plus real turns through
the connected subscription: text in Russian and English, multiline input, file
tools, continuation and native resume, stop/error recovery and card delivery.
Passing synthetic tests alone does not satisfy that release acceptance.
