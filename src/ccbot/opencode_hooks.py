"""Install a dedicated native plugin that publishes OpenCode session bindings."""

import json
import os
from pathlib import Path


def install_hooks(command: str, settings_file: Path | None = None) -> int:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = settings_file or base / "opencode/plugins/ccbot.js"
    source = """import { spawn } from "node:child_process";
export const CCBotPlugin = async ({ directory }) => ({
  // Lifecycle events are shared by other OpenCode windows in this project.
  // This hook runs only when this instance accepts an actual prompt.
  "chat.message": async ({ sessionID }) => {
    if (!sessionID || !process.env.TMUX_PANE || process.env.CCBOT_AGENT_BACKEND !== "opencode") return;
    await new Promise((resolve) => {
      const child = spawn(COMMAND, ["hook", "--backend", "opencode"], {
        stdio: ["pipe", "ignore", "ignore"], timeout: 5000,
      });
      child.on("error", resolve);
      child.on("close", resolve);
      child.stdin.on("error", () => {});
      child.stdin.end(JSON.stringify({session_id: sessionID, cwd: directory,
        hook_event_name: "UserPromptSubmit", opencode_origin: "chat.message"}));
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
