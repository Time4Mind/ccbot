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

这个 fork 有意识地与 upstream `ccbot` 分歧在以下几个不可妥协的
方面:

- **仅 DM。** 没有超级群组、没有论坛主题、没有 thread 路由。机器人
  只能看到与 allowlist 中 Telegram user-id 的私聊 1-1 DM。
- **个人 + allowlist 门禁。** `ALLOWED_USERS` 通常只有一个 Telegram
  数字 id。填多个则视为*共享工作区*:会话池是全局的,每个 claude
  事件都会扇出到每位被允许用户各自的私聊(各自的实时卡片、各自的
  切换器)。这不是多租户 — 所有人都能看到全部内容。来自非 allowlist
  发送者的任何消息都会被静默丢弃(无回复、无 callback 提示)——在
  外人看来机器人是「死的」。
- **仅 bypass 模式。** `claude` 启动时带 `--dangerously-skip-permissions`
  。Telegram 中没有 permission 提示中继 — 如果你不信任模型对主机的
  完全访问权限,请使用 upstream。(bypass 覆盖不到的残余 Yes/No
  提示 — 例如 WebFetch 的域名信任 — 会以键盘形式出现,也可以通过
  `设置 → 自动确认` 自动回答。)
- **多会话 + 内联切换器。** 一个用户可以在同一 DM 中拥有多个会话;
  最新机器人消息下方的内联键盘在它们之间切换。
- **优先 rich 消息。** 输出以 Bot API 10.1 rich message 发出(原生
  markdown:≤ 20 列的 GFM 表格、标题、`<details>`、脚注、公式),
  失败时回退到 MarkdownV2 管道(`telegramify-markdown`),再失败
  则回退到纯文本。总开关:`CCBOT_RICH_MESSAGES=off`。upstream 用
  HTML。
- **基于 hook 的会话跟踪。** Claude Code 的 `SessionStart` +
  `UserPromptSubmit` hook 写入 `session_map.json`;监控器轮询它。
  不依赖进程树检查或 claude SDK。
- **语音 — 本地优先。** `whisper.cpp`(默认)或 macOS 上通过 PyObjC
  的 Apple Speech — 运行不需要 API key。

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
ccbot hook --install            # 一次性:注册 Claude Code hook
ccbot                           # 前台;生产环境用 systemd 单元
```

完整的 Linux 逐步安装(面向 AI agent 编写)见 `doc/install-linux.md`。

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
| `QUOTA_ALERT_POLL_INTERVAL` | `10m`        | 实时 `/usage` 弹窗的采样间隔 |
| `VOICE_BACKEND`             | `auto`       | `auto` / `whisper` / `apple` / `off` |
| `WHISPER_MODEL_PATH`        | `~/.ccbot/models/ggml-medium-q8_0.bin` | whisper.cpp 模型(回退到已存在的 `ggml-medium.bin`) |
| `WHISPER_LANG_MODEL_PATH`   | `~/.ccbot/models/ggml-tiny.bin` | 语言检测预处理用的 tiny 模型 |
| `WHISPER_LANG_DEFAULT`      | `ru`         | 检测置信度不足时假定的语言 |
| `WHISPER_THREADS`           | `6`          | `whisper-cli` 的线程数(它自己的默认值是 4) |
| `BG_STATUS_MAX`             | `4`          | bg-status 面板最多显示的徽章数;多余的折叠为 `+N more` |
| `CARD_EDIT_LAG`             | `2.0`        | live-card 编辑的合并窗口(秒) |
| `CCBOT_RICH_MESSAGES`       | `on`         | `off` 关闭 Bot API 10.1 rich 消息(只用 MarkdownV2) |
| `CCBOT_HOST`                | hostname     | 部署标签,以 `CCBOT_HOST` 导出到会话中 |
| `TG_PROXY_URL`              | _(未设)_     | Bot API 出站代理(`socks5://…` 或 `http://…`) |

完整列表在 `.env.example` 和 `doc/dm-multisession-spec.md` § 12。
个人 UI 偏好(卡片大小、通知、语音后端、语言……)不是 env 变量,
它们在 `≡ 菜单 → Settings` 里,见下文。

## Hook 设置

机器人通过两个 Claude Code hook 跟踪 tmux-窗口 ↔ Claude-session 的
映射:`SessionStart` 捕获每个新的 claude 进程,`UserPromptSubmit`
在每次提交提示时修复过期映射(覆盖 `/resume`、`/clear` 以及机器人
重启时的竞态)。一次性自动安装:

```bash
ccbot hook --install
```

安装器按事件幂等 — 在旧的仅 `SessionStart` 安装上重跑,只会补上
缺失的那一条。

或手动添加到 `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ],
    "UserPromptSubmit": [
      { "hooks": [{ "type": "command", "command": "ccbot hook", "timeout": 5 }] }
    ]
  }
}
```

## 使用

机器人在 Telegram 的 `/`-菜单中提供少量 slash 命令,加上最新机器人
消息下方的内联 `≡ Menu` 按钮:

| 命令       | 效果 |
| ---------- | ---- |
| `/menu`    | 打开内联 ≡ Menu |
| `/help`    | 简短指南(带内联导航的分节文档) |
| `/history` | 活动会话的完整转录(分页) |
| `/done`    | 关闭活动会话(标记为「完成」并归档) |

Claude Code 自己的选择器(`/model`、`/effort`、`/compact`、
`/memory`)会转发到活动会话,并与上面几条一起发布。另有几个命令
输入时可用,但不出现在 `/`-菜单里:`/new`、`/kill`、`/stop`、
`/archive`、`/screenshot`、`/usage`、`/health`、`/login`。

**`/login` —— 用手机给 Claude 重新授权。** `claude` 背后的 OAuth 登录
过期后,所有会话都会开始报错,通常没有电脑就修不了。机器人本身不需要
Claude 授权,所以它会发现失败、推送 🔐 通知,并由 `/login` 完成整个
交换:机器人给你链接,你在手机浏览器里确认(重定向到
`platform.claude.com`,不是 localhost 回调,所以不需要隧道),再把页面
显示的码作为普通消息发回来。机器人把它喂给等待中的进程、确认新的期限,
并删除你那条带码的消息。随后它会重新发出活动会话的卡片(没有活动会话
则发菜单),让你直接接着干,而不用往上翻过整个授权过程。

该通知由 Claude Code 自己的错误条目触发(`isApiErrorMessage` /
`authentication_failed`),而不是靠错误文本 —— 只是谈论登录失效的会话
不会触发它。一次新的登录会把凭据期限往后推约 30 天;真正重要的就是这个
期限,因为 refresh token 轮换并不会移动它。

其余动作藏在内联菜单后面:`Sessions`、`Archive`、`Status`、`New`、
`Settings`。🧑‍💻 *Shot*(终端截图)按钮住在主视图的控制行和
*菜单 → Sessions* 中 —— 紧邻 *Kill* 和 *Clear*,所以它始终在
transcript 表面触手可及。多数用户一旦发现菜单,就再也不打 slash
命令。

### 会话与切换器

向 DM 发送任何文本即可创建第一个会话 — 机器人会打开目录浏览器,
你选择项目,tmux 窗口中启动 `claude`。后续 DM 中的文本路由到**活动**
会话。

会话最初以目录名命名,在第一条 ≥ 20 字符的消息到达时由一次性
Haiku 调用重命名一次(两个词概括意图 — `token budget`)。在
*设置 → Haiku 会话命名* 中关掉它,即可保留目录名并零 token 开销。

最新机器人消息携带内联会话切换器(`▷ session-A · session-B`),
最底行是一对 `[+ new] [≡ 菜单]`:两个「去别处」的按钮并排放置,
此槽位在不同视图间保持稳定(在 *菜单 → Sessions* / *Archive* 中,
这个槽位换成 `[+ new] [Back]`)。

切换器按钮按**从旧到新**排列:一个会话在其整个生命周期内都保持同一
槽位,新建的会话追加到右侧,切换时的肌肉记忆不会被打乱。从归档恢复
的会话会作为最新的按钮重新进入,而不是回到原来的槽位。`/screenshot`
下的紧凑切换器使用相同的顺序。

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

后台(非活动)会话**没有自己的实时卡片** — 它们不会编辑卡片,也
不会在聊天里弹出 AskUserQuestion 提示。它们的状态以活动会话卡片
底部的紧凑面板形式呈现:

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

除了徽章,后台会话还可以在*状态跃迁*时推送一行简讯
(`✅ [scraper] task complete`)。*设置 → 🔔 通知* 下有三个独立开关,
默认全开:`Bg: 任务完成`、`Bg: 错误`、`Bg: 需要操作`。关掉它们,
后台工作就完全静默。

### 实时卡片

每个活动会话拥有一条机器人持续编辑的实时卡片 — 表头、分页正文、
后台面板、底部键盘。你每发一条消息,卡片就会重新发布到你的消息
下方(现在只有这一种行为;旧的 `Card position` 设置已移除)。后台
面板上方会打印该会话的 `context: N%` — 由 JSONL 计算出的 Claude
Code `/context` 近似值,通常与弹窗相差 ±10 % 以内。

卡片相关开关在 *设置 → 🃏 卡片 / 视图*:

| 设置 | 默认 | 效果 |
| ---- | ---- | ---- |
| `卡片历史` | `20` | 从 JSONL 预加载进新卡片的 end-of-turn 边界数(机器人重启后仍在) |
| `页面大小` | `20` 行 | 每页最多行数;长正文按段落/句子边界跨页切分 |
| `内联截图` | `off` | 卡片变为图片 + 说明文字,图片是实时面板渲染(说明限 1024 字符,需相应调小页面大小) |
| `预览` | `economical` | 切换器预览的详细程度 |
| `实时延迟` | `4s` | 预览更新的合并窗口 |

Telegram 聊天头部的 **`正在输入…`** 指示由真实的 claude 事件驱动。
只要活动会话仍在发出事件(tool 调用、思考、文本),`正在输入…` 就
持续显示;空闲会话会让它在 Telegram 的 ~5 秒窗口内自然消失。

### 其他设置

*≡ 菜单 → Settings* 把设置分成五类:🃏 卡片 / 视图、🔔 通知、
🎙 语音、🖥 本地终端、⚙ 行为与语言。值得知道的:

- **自动确认**(默认 `off`)— 自动回答 `--dangerously-skip-permissions`
  覆盖不到的那些交互式 Yes/No 提示(WebFetch 域名信任之类)。若
  自动 Yes 没能消除提示,机器人会升级为手动键盘而不是死循环。
- **本地终端**(`off` / `manual` / `auto`)— 打开一个附着到会话
  tmux 窗口的原生 Terminal.app / iTerm2 / Linux 终端模拟器窗口,
  以便你用手驱动同一个会话。`manual` 只显示 🖥 *Term* 按钮;
  `auto` 还会为每个新会话自动开一个。kill 会话时会关掉它开的标签页。
- **每周重置**— Anthropic 每周配额窗口翻滚的那一天;决定
  *菜单 → Status* 里的 `%/天` 消耗速率。
- **语言** — 机器人自身 UI 字符串的 `en` / `ru` / `zh`。

### 配额与状态

*≡ 菜单 → 📊 Status* 通过专用的 `ccbot-usage` tmux 窗口采样
Claude Code 自己的 `/usage` 弹窗,并紧凑地渲染出来:

```
Claude Code
🟡 5h: 62% · 12.4%/h · 17:00
🟢 week: 28% · 4.0%/d · Mon 17:00
🟢 week (Sonnet): 12% · Mon 17:00
```

同一个轮询每隔 `QUOTA_ALERT_POLL_INTERVAL` 在后台运行,当 5 小时
或每周窗口越过 50 / 75 / 90 % 时推送提醒。只有稳定读数才会发布,
所以渲染到一半的弹窗不会触发幻影告警。

### 语音和媒体

- **语音消息** 在本地转写(whisper.cpp / Apple Speech),并以你
  键入的方式路由到活动会话。转写期间卡片上显示 pending 标记,完成
  后原地替换为转写文本,你可以验证 Claude 收到了什么。在 arm64
  参考主机上一条语音端到端约 9 秒:量化的 `ggml-medium-q8_0`
  (比 fp16 快 1.8 倍,ru/en 样本转写结果一致)加上 `ggml-tiny`
  的语言检测预处理 — 后者让正式那遍能钉住 `-l` 只编码一次。
  缺二进制或模型?*设置 → 🎙 语音* 一键安装(编译 whisper.cpp、
  下载两个模型)。
- **照片和文档** 落到 `<workdir>/.ccbot-inbox/`,Claude 通过 tmux
  收到通知。文件在上传 24 小时后自动清理。
- **出站文件** 是反方向的按需通道:会话运行
  `ccbot send-file <路径> [--caption 文本]`,机器人立刻把文件发进
  DM(图片扩展名走 `sendPhoto`,其余走 `sendDocument`)。该命令按
  目标聊天打印成功/失败行,Claude 能看到是否送达。
- **带媒体的转发消息**(包含视频 / GIF / 贴纸但有 caption 文本的
  频道帖子) — caption 加上任何隐藏的 `text_link` URL 都会被提取
  并路由到活动会话,前缀为 `[forwarded from @channel]`。媒体本体
  被丢弃 — Claude 处理不了。

## 架构

完整模块图在 `.claude/rules/architecture.md`。一览:

```
src/ccbot/
├── main.py                 — CLI entry point (`ccbot`, `ccbot hook`, `ccbot send-file`)
├── config.py               — env-var 加载器(singleton)
├── session.py              — Session + SessionManager (state.json)
├── session_monitor.py      — JSONL polling, NewMessage callbacks
├── transcript_parser.py    — JSONL turn 解析
├── terminal_parser.py      — interactive UI + status line 检测
├── tmux_manager.py         — libtmux 包装
├── rich.py                 — Bot API 10.1 rich 消息(原生 markdown)
├── markdown_v2.py          — MD → Telegram MarkdownV2(回退路径)
├── telegram_sender.py      — split_message 在 4096 字符限制处分割
├── transcribe.py           — 语音 → 文本 dispatcher
├── voice_install.py        — whisper.cpp + 模型自动安装器
├── send_file.py            — `ccbot send-file` 出站投递
├── local_terminal.py       — 原生终端挂载助手
├── usage.py                — token 聚合器、context %、提醒逻辑
├── i18n.py                 — en / ru / zh UI 字符串
├── bot/                    — Telegram-facing handlers(每文件 ≤ 600 LOC)
│   ├── app.py              — Application bootstrap, post_init / post_shutdown
│   ├── messages.py         — text / voice / photo / document / forward
│   ├── session_events.py   — claude → TG dispatch
│   ├── commands/           — slash 命令本体
│   └── callbacks/          — 每个 CB_* 前缀一个文件
└── handlers/
    ├── notifications.py    — live cards + push events
    ├── card_model.py       — 卡片状态 / 渲染 / 分页模型层
    ├── bg_status.py        — 后台会话状态面板
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
| `ccbot.lock`        | 运行中机器人持有的独占 flock;第二次启动以退出码 1 拒绝 |

## 可靠性

- **单实例。** `main.py` 在整个生命周期内持有 `ccbot.lock` 的独占
  `flock`,所以监督进程重启撞上手动启动时,不会出现两个机器人争抢
  `getUpdates`。
- **长轮询看门狗。** 基于线程的存活检查会发现悄悄卡死的长轮询;
  持续断网时进程会主动退出而不是哑着不动 — 网络恢复后由
  supervisor/systemd 重新拉起。
- **启动恢复。** tmux 窗口仍在的会话会重新挂接,消失的标记为
  `lost`(带 `Restore` 按钮),而没有任何绑定的 tmux 窗口只记录为
  orphan 警告,不会被杀掉。

## 部署

systemd 单元在 `scripts/ccbot.service`;上行链路不稳定的主机可以改
跑 `scripts/ccbot-supervisor.sh` — 它在每次启动前等待网络,并带
backoff 重启。对于无法直接访问 `api.telegram.org` 的 VPS 主机,
参见 `doc/deploy.md` 的 SSH-tunnel recipe(`TG_PROXY_URL`)。完整的
Linux 安装步骤(面向 AI agent)在 `doc/install-linux.md`。

## 贡献

参见 [CONTRIBUTING.md](CONTRIBUTING.md)。简而言之:与 DM-only /
个人 allowlist / bypass-only 不变量一致的 PR 都欢迎。CI 必须绿;
pre-commit hook 必须通过;一个 PR 一个目的。

## 安全

参见 [SECURITY.md](SECURITY.md) 了解威胁模型和报告流程。漏洞通过
GitHub Security Advisories 报告,不要发到公共 issue。

## 许可

参见 [LICENSE](LICENSE)。
