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
| `OPENCODE_MODEL` | `openai/gpt-6-luna` | Explicit initial and naming model |
| `OPENCODE_SESSION_DB_PATH` | `$XDG_DATA_HOME/opencode/opencode.db` | Optional native database override |

Workers also accept `CCBOT_OPENCODE_COMMAND`. They advertise OpenCode only when
its executable and OpenAI OAuth configuration are present. Worker installation
and authentication are local to each node.

CCBot launches the native TUI in its own window and uses `--session ses_...` for
native resume. Input is gated on the native composer and a running OpenCode
process. `/model` maps to `/models`, `/clear` maps to `/new`; native dialogs remain
available through terminal and arrow controls. Model availability follows the
connected provider. The adapter does not invent a separate effort selector.

OpenCode owns its database. CCBot uses SQLite `mode=ro`, `query_only` and a
consistent transaction; it never migrates or updates native tables. Visible
messages, reasoning, paired tools, terminal errors and usage are projected to
an append-only CCBot journal under `$CCBOT_DIR/opencode-transcripts/`. Synthetic
and model-only inputs are hidden. Stable identities prevent replay duplicates;
an interrupted append repairs only the CCBot journal. Histories, archive restore,
cross-backend handoff and worker events retain native backend identity.

Usage comes from native completed messages. Account quota and context capacity
remain unknown when the native source does not supply them.

Release acceptance requires repository checks plus real Luna-6 turns through
the connected subscription: text in Russian and English, multiline input, file
tools, continuation and native resume, stop/error recovery and card delivery.
Passing synthetic tests alone does not satisfy that release acceptance.
