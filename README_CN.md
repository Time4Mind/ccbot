# ccbot

[![test](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/test.yml)
[![secrets-scan](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml/badge.svg)](https://github.com/Time4Mind/ccbot/actions/workflows/secrets-scan.yml)

[English README](README.md) · [Русская документация](README_RU.md)

一个个人 Telegram 机器人,将私聊 1-1 DM 桥接到运行在 tmux 中的 N 个并行
Claude Code 会话。一位用户、N 个会话、最新机器人消息下方的一个内联切换器。

## 为什么

Claude Code 运行在终端里。离开桌子就失去了可见性 — 但会话仍在继续。
ccbot 让你可以:

- **在工作中途从电脑切换到手机。** Claude 正在做重构 — 你出去散步,
  继续在 Telegram 上监控和回复。
- **随时切换回电脑。** 会话存活在真实的 tmux 窗口里,`tmux attach`
  直接把你带回终端,完整的滚动历史和上下文都还在。
- **并行运行多个会话。** 每个会话都有自己的 tmux 窗口和自己的
  `claude` 进程。在 Telegram 中切换活动会话不会暂停其他任何会话。

机器人是 tmux 之上的一层薄薄的控制层 — 你的 Claude Code 进程始终
在原地。ccbot 只负责读取它的输出并发送按键。

## 与 upstream 的区别

这个 fork(`feat/dm-multisession`)有意识地与 upstream `ccbot` 分歧
在以下几个不可妥协的方面:

- **仅 DM。** 没有超级群组、没有论坛主题、没有 thread 路由。机器人
  只能看到与一个 allowlist 中 Telegram user-id 的私聊 1-1 DM。
- **单用户。** `ALLOWED_USERS` 应该恰好包含一个 Telegram 数字 id。
  多租户部署不在范围内。
- **仅 bypass 模式。** `claude` 启动时带 `--dangerously-skip-permissions`
  。Telegram 中没有 permission 提示中继 — 如果你不信任模型对主机的
  完全访问权限,请使用 upstream。
- **多会话 + 内联切换器。** 一个用户可以在同一 DM 中拥有多个会话;
  最新机器人消息下方的内联键盘在它们之间切换。
- **MarkdownV2** 渲染管道(通过 `telegramify-markdown`),解析失败时
  自动 fallback 到纯文本。upstream 用 HTML。
- **基于 hook 的会话跟踪。** Claude Code 的 `SessionStart` hook 写入
  `session_map.json`;监控器轮询它。不依赖进程树检查或 claude SDK。
- **语音 — 本地优先。** `whisper.cpp`(默认)或 macOS 上通过 PyObjC
  的 Apple Speech。OpenAI fallback 存在但默认关闭 — 运行不需要 API key。

完整的设计动机在 `doc/dm-multisession-spec.md`。实现地图在
`doc/dm-multisession-plan.md`。

## 先决条件

- **tmux** 在 `PATH` 中
- **Claude Code** CLI(`claude`)已用 Max 订阅登录
- **Python 3.12+**
- **uv**(推荐)用于依赖管理
- macOS(Apple Silicon)或 Linux arm64

可选:

- **`ffmpeg`** + **`whisper-cli`** 用于本地语音转写
- **`pyobjc-framework-Speech`** 用于原生 Apple Speech 后端
  (`uv sync --extra apple-speech`)

## 快速开始

```bash
git clone https://github.com/Time4Mind/ccbot.git
cd ccbot
uv sync
cp .env.example ~/.ccbot/.env   # 填入 TELEGRAM_BOT_TOKEN + ALLOWED_USERS
ccbot hook --install            # 一次性:注册 Claude Code SessionStart hook
ccbot                           # 前台;生产环境用 systemd 单元
```

## 配置

`~/.ccbot/.env`(或 `./.env`)中的必需 env 变量:

| 变量                   | 描述                                              |
| ---------------------- | ------------------------------------------------- |
| `TELEGRAM_BOT_TOKEN`   | 来自 [@BotFather](https://t.me/BotFather) 的 token |
| `ALLOWED_USERS`        | 一个 Telegram 数字 user-id                        |

最常调整的可选项:

| 变量                        | 默认值       | 效果 |
| --------------------------- | ------------ | ---- |
| `CCBOT_DIR`                 | `~/.ccbot`   | 配置和状态目录 |
| `TMUX_SESSION_NAME`         | `ccbot`      | 装载所有 session 窗口的 tmux 会话 |
| `CLAUDE_COMMAND`            | `claude`     | 启动会话使用的二进制 |
| `CLAUDE_FLAGS`              | `--dangerously-skip-permissions` | 附加给 `claude` 的 flag |
| `SESSION_IDLE_TTL`          | `4h`         | 闲置多久后 active → archived |
| `ARCHIVE_PURGE_AFTER`       | `14d`        | 归档会话从 state 中清除的时长 |
| `QUOTA_ALERT_POLL_INTERVAL` | `5m`         | 实时 `/usage` 弹窗的采样间隔 |
| `VOICE_BACKEND`             | `auto`       | `auto` / `whisper` / `apple` / `off` |
| `WHISPER_MODEL_PATH`        | `~/.ccbot/models/ggml-medium.bin` | whisper.cpp 模型 |
| `BG_NOTIFY_MODE`            | `separate`   | `separate`(每会话一张卡)或 `footer` |
| `TG_PROXY_URL`              | _(未设)_     | Bot API 出站代理(`socks5://…` 或 `http://…`) |

完整列表在 `doc/dm-multisession-spec.md` § 14。

## Hook 设置

机器人通过 Claude Code 的 `SessionStart` hook 跟踪 tmux-窗口 ↔
Claude-session 的映射。一次性自动安装:

```bash
ccbot hook --install
```

或手动添加到 `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ]
  }
}
```

## 使用

机器人在 Telegram 的 `/`-菜单中提供少量 slash 命令,加上最新机器人
消息下方的内联 `≡ Menu` 按钮:

| 命令      | 效果 |
| --------- | ---- |
| `/menu`   | 打开内联 ≡ Menu |
| `/help`   | 简短指南(带内联导航的分节文档) |
| `/health` | 运行时间、tmux 状态、队列、延迟、计数器 |
| `/done`   | 关闭活动会话(标记为「完成」并归档) |

其余动作藏在内联菜单后面:`List`、`Status`、`History`、`Shot`、
`New`、`Archive`、`Settings`。多数用户一旦发现菜单,就再也不打 slash
命令。

### 会话与切换器

向 DM 发送任何文本即可创建第一个会话 — 机器人会打开目录浏览器,
你选择项目,tmux 窗口中启动 `claude`。后续 DM 中的文本路由到**活动**
会话。

最新机器人消息携带内联会话切换器(`▷ session-A · session-B · + new`)。
点击非活动会话按钮会切换活动会话并显示上下文预览;点击活动按钮是
no-op。引用回复(Telegram quote)非活动会话的机器人消息,会把那一
条回复路由到该会话,但不更改活动会话。

### Token 提醒

两个流,都作为单独的推送:

- **Per-session token 提醒。** 三个阈值(默认 100k/200k/400k,
  Settings → Token alerts 中以 50k 为步长调整)。每会话每阈值触发
  一次。
- **5h / 周 / 周-Sonnet 配额提醒。** 后台任务每
  `QUOTA_ALERT_POLL_INTERVAL` 采样一次实时 `/usage` 弹窗,当百分比
  跨过 stoplight emoji 同样的 50/75/90 % 时推送。

### 语音和媒体

- **语音消息** 在本地转写(whisper.cpp / Apple Speech),并以你
  键入的方式路由到活动会话。
- **照片和文档** 落到 `<workdir>/.ccbot-inbox/`,Claude 通过 tmux
  收到通知。文件在上传 24 小时后自动清理。

## 架构

完整模块图在 `.claude/rules/architecture.md`。一览:

```
src/ccbot/
├── main.py                 — CLI entry point (`ccbot`, `ccbot hook`)
├── config.py               — env-var 加载器(singleton)
├── session.py              — Session + SessionManager (state.json)
├── session_monitor.py      — JSONL polling, NewMessage callbacks
├── transcript_parser.py    — JSONL turn 解析
├── terminal_parser.py      — interactive UI + status line 检测
├── tmux_manager.py         — libtmux 包装
├── markdown_v2.py          — MD → Telegram MarkdownV2
├── telegram_sender.py      — split_message 在 4096 字符限制处分割
├── transcribe.py           — 语音 → 文本 dispatcher
├── usage.py                — token 聚合器 + 提醒逻辑
├── i18n.py                 — en / ru / zh UI 字符串
├── bot/                    — Telegram-facing handlers(每文件 ≤ 600 LOC)
│   ├── app.py              — Application bootstrap, post_init / post_shutdown
│   ├── messages.py         — text / voice / photo / document / forward
│   ├── session_events.py   — claude → TG dispatch
│   ├── commands/           — slash 命令本体
│   └── callbacks/          — 每个 CB_* 前缀一个文件
└── handlers/
    ├── notifications.py    — live cards + push events
    ├── archive.py          — /archive 页面渲染 + 闲置扫描
    ├── quota_alerts.py     — 后台 /usage poll
    ├── interactive_ui.py   — AskUserQuestion / ExitPlanMode
    ├── menu.py             — 内联键盘组装
    └── …
```

状态保存在 `$CCBOT_DIR`(默认 `~/.ccbot/`)下:

| 文件                | 内容 |
| ------------------- | ---- |
| `state.json`        | sessions, active_sessions, window states, user settings |
| `session_map.json`  | hook 生成的 tmux-窗口 → claude-session 映射 |
| `monitor_state.json`| per-JSONL byte offsets(防止重启时重复通知) |

## 部署

systemd 单元在 `scripts/ccbot.service`。对于无法直接访问
`api.telegram.org` 的 VPS 主机,参见 `doc/deploy.md` 的 SSH-tunnel
recipe(`TG_PROXY_URL`)。完整的 Linux 安装步骤(面向 AI agent)
在 `doc/install-linux.md`。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。简而言之:与 DM-only /
single-user / bypass-only 不变量一致的 PR 都欢迎。CI 必须绿;
pre-commit hook 必须通过;一个 PR 一个目的。

## 安全

参见 [SECURITY.md](SECURITY.md) 了解威胁模型和报告流程。漏洞通过
GitHub Security Advisories 报告,不要发到公共 issue。

## 许可

参见 [LICENSE](LICENSE)。
