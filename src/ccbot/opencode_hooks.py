"""Install a dedicated native plugin that publishes OpenCode session bindings."""

import json
import os
from pathlib import Path


def install_hooks(command: str, settings_file: Path | None = None) -> int:
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    path = settings_file or base / "opencode/plugins/ccbot.js"
    source = """import { spawn } from "node:child_process";
const denyAsks = (value) => {
  if (value === "ask") return "deny";
  if (Array.isArray(value)) return value.map(denyAsks);
  if (value && typeof value === "object")
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, denyAsks(v)]));
  return value;
};
const policyObject = (value) => typeof value === "string" ? { "*": denyAsks(value) } : denyAsks(value ?? {});
const noQuestions = { question: "deny", plan_enter: "deny", plan_exit: "deny" };
export const CCBotPlugin = async ({ directory }) => ({
  config: async (cfg) => {
    if (process.env.CCBOT_AGENT_BACKEND !== "opencode") return;
    const permission = policyObject(cfg.permission);
    // Replace native ask defaults without widening explicit allow/deny rules.
    if (!("*" in permission)) {
      permission.doom_loop ??= "deny";
      permission.external_directory = typeof permission.external_directory === "string"
        ? permission.external_directory : { "*": "deny", ...permission.external_directory };
      if (typeof permission.read !== "string" && !(permission.read && "*" in permission.read))
        permission.read = { "*.env": "deny", "*.env.*": "deny", "*.env.example": "allow", ...permission.read };
    }
    cfg.permission = { ...permission, ...noQuestions };
    cfg.agent ??= {};
    for (const name of new Set(["build", "plan", ...Object.keys(cfg.agent)])) {
      const agent = cfg.agent[name] ??= {};
      agent.permission = { ...policyObject(agent.permission), ...noQuestions };
    }
  },
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
