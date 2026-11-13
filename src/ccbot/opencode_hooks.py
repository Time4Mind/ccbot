"""Install a dedicated native plugin that publishes OpenCode session bindings."""

import json
import os
from pathlib import Path


def install_hooks(command: str, settings_file: Path | None = None) -> int:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = settings_file or base / "opencode/plugins/ccbot.js"
    source = """import { spawn } from "node:child_process";
export const CCBotPlugin = async ({ directory }) => ({
  event: async ({ event }) => {
    if (!process.env.TMUX_PANE || process.env.CCBOT_AGENT_BACKEND !== "opencode") return;
    let id;
    if (event.type === "session.created") id = event.properties.info.id;
    if (event.type === "message.updated" && event.properties.info.role === "user")
      id = event.properties.info.sessionID;
    if (!id) return;
    await new Promise((resolve) => {
      const child = spawn(COMMAND, ["hook", "--backend", "opencode"], {
        stdio: ["pipe", "ignore", "ignore"], timeout: 5000,
      });
      child.on("error", resolve);
      child.on("close", resolve);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify({session_id: id, cwd: directory,
        hook_event_name: "UserPromptSubmit"}));
    });
  },
});
""".replace("COMMAND", json.dumps(command))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        return 0
    except OSError:
        return 1
