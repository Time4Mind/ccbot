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
  多租户部署不在范围内。来自非 allowlist 发送者的任何消息都会被
  静默丢弃(无回复、无 callback 提示)——在外人看来机器人是「死的」。
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
| `BG_STATUS_MAX`             | `4`          | bg-status 面板最多显示的徽章数;多余的折叠为 `+N more` |
| `CARD_EDIT_LAG`             | `2.0`        | live-card 编辑的合并窗口(秒) |
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

其余动作藏在内联菜单后面:`List`、`Status`、`History`、`New`、
`Archive`、`Settings`。🧑‍💻 *Shot*(终端截图)按钮现在住在主视图
的控制行和 *菜单 → List* 中 —— 紧邻 *Kill* 和 *Clear*,
所以它始终在 transcript 表面触手可及。多数用户一旦发现菜单,就再
也不打 slash 命令。

### 会话与切换器

向 DM 发送任何文本即可创建第一个会话 — 机器人会打开目录浏览器,
你选择项目,tmux 窗口中启动 `claude`。后续 DM 中的文本路由到**活动**
会话。

最新机器人消息携带内联会话切换器(`▷ session-A · session-B`),
最底行是一对 `[+ new] [≡ 菜单]`:两个「去别处」的按钮并排放置,
此槽位在不同视图间保持稳定(在 *菜单 → List* / *Archive* 中,
这个槽位换成 `[+ new] [Back]`)。

点击非活动会话按钮会**把该会话的完整转录历史画到 carrier-消息上**
并同时切换活动会话。分页按钮 (◀ Older / Newer ▶) 本身就是「翻看
历史」的入口,因此菜单中不再有独立的「历史」条目;它们下方仍
保留底部键盘。点击已活动的按钮是 no-op。`/screenshot` 中的 `Back`
重新发布实时卡片。

引用回复(Telegram quote)非活动会话的机器人消息,会把那一条回复
路由到该会话,但不更改活动会话。

*菜单 → Archive* 显示带编号的历史会话列表,每行两个按钮。每行
携带一段简短描述(Claude 自己的 `type=summary` 条目,或第一条
用户消息),这样一眼就能看出会话是关于什么的。点击会话,carrier
会画出直接从磁盘 JSONL 读取的真实转录;*Restore* / *Delete*
保留在底部。

### 后台会话

后台(非活动)会话**在聊天中保持静默** — 不发出 live-card 编辑、
推送通知或 AskUserQuestion 提示。它们的状态仅以活动会话卡片底部的
紧凑面板形式呈现:

```
🟦 session-A ⏳        ← 后台运行中
🟪 scraper   ✅        ← 完成
🟧 chores    ❌        ← 出错
🟨 frontend  ❓        ← 需要用户操作(AskUserQuestion / permission)
```

面板在活动卡片的编辑之间「黏住」,这样已完成的后台会话不会丢失在
长 tool-log 之上。在切换器中点击该徽章对应的会话,会把它从面板中
移除(你「看到了」)。如果徽章是 `❓`,切换器点击会画出存好的
AskUserQuestion / ExitPlanMode 提示,带和前台提示相同的箭头 /
Enter / Esc 键盘。

### Live-card 用户体验调整

全新的 live-card 会从会话 JSONL 转录中预加载最多 `CARD_SEED_TURNS`
(默认 20)个最近的 end-of-turn 边界,以便机器人重启后历史不会
消失。

`Settings → Card position` 控制你发出的文本与 live-card 的关系:
- `push` — 保持不变(你的消息把卡片推上去;默认)
- `delete` — 机器人删掉你的消息,卡片留作最新一条
- `repost` — 机器人在你消息下方重新发送卡片,并删除旧的

Telegram 聊天头部的 **`正在输入…`** 指示由真实的 claude 事件驱动。
只要活动会话仍在发出事件(tool 调用、思考、文本),`正在输入…` 就
持续显示;空闲会话会让它在 Telegram 的 ~5 秒窗口内自然消失。

### 语音和媒体

- **语音消息** 在本地转写(whisper.cpp / Apple Speech),并以你
  键入的方式路由到活动会话。机器人回复转写文本,这样你可以验证
  Claude 收到了什么。
- **照片和文档** 落到 `<workdir>/.ccbot-inbox/`,Claude 通过 tmux
  收到通知。文件在上传 24 小时后自动清理。
- **带媒体的转发消息**(包含视频 / GIF / 贴纸但有 caption 文本的
  频道帖子) — caption 加上任何隐藏的 `text_link` URL 都会被提取
  并路由到活动会话,前缀为 `[forwarded from @channel]`。媒体本体
  被丢弃 — Claude 处理不了。

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
