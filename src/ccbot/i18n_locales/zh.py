"""Chinese translation table for the Telegram UI."""

from __future__ import annotations

ZH: dict[str, str] = {
    "voice.not_delivered": (
        "🎙 语音未送达会话 —— 会话中有待处理的提示（授权/提问），"
        "转写文本无法送入。请先回应该提示，然后重新发送语音消息。"
    ),
    "voice.download_failed": (
        "🎙 语音未送达会话：Telegram 在 {attempts} 次尝试后仍无法提供音频文件。"
        "请重新发送语音消息。"
    ),
    "voice.transcription_failed": "🎙 语音无法识别且未送达会话。请重新发送。",
    "voice.transcribing": "🎙 正在转写语音消息…",
    "voice.queued_dropped": "之后发送的消息也未送达会话。",
    "btn.stop": "⏹ 停止",
    "btn.kill": "💀 终止",
    "btn.clear": "🧹 清空",
    "btn.menu": "≡ 菜单",
    "btn.term": "🖥 终端",
    "btn.back": "← 返回",
    "btn.cancel": "× 取消",
    "btn.login": "🔐 登录",
    # Claude re-authentication (/login)
    "auth.expired": (
        "🔐 *Claude 授权已失效*\n\n"
        "在重新登录之前,这台主机上的所有会话都会报错。机器人本身没事 —— "
        "它可以带你走完流程。\n\n"
        "发送 /login(或点下面的按钮):我给你链接,你在浏览器里确认,"
        "然后把码发回这里。"
    ),
    "auth.login.starting": "🔐 正在启动登录流程…",
    "auth.login.url": (
        "🔐 *第 1/2 步* —— 打开并确认:\n\n"
        "{url}\n\n"
        "*第 2/2 步* —— 页面会显示一个码。把它作为普通消息发到这里。"
        "链接 15 分钟内有效。"
    ),
    "auth.login.no_url": (
        "❌ 没能从 CLI 拿到登录链接。再试一次 /login;如果一直失败,"
        "请在主机上执行 `claude auth login`。"
    ),
    "auth.login.ok": (
        "✅ *已登录。* 授权已延长到 {deadline}。\n\n"
        "之前报错的会话在下一条消息就会恢复。"
    ),
    "auth.login.failed": "❌ 验证码未被接受:{detail}\n\n发送 /login 重试。",
    "auth.login.cancelled": "已取消登录。",
    "auth.codex.device": (
        "🔐 *Codex 登录*\n\n"
        "1. 打开 {url}\n"
        "2. 输入代码: `{code}`\n\n"
        "机器人会自动检测授权;无需把代码发到这里。代码约 15 分钟内有效。"
    ),
    "auth.codex.no_device_code": (
        "❌ Codex 未提供设备代码。请确认已安装最新 Codex CLI,然后发送 /login 重试。"
    ),
    "auth.codex.ok": "✅ *Codex 已授权。* 现在可以创建和恢复会话。",
    "auth.codex.failed": "❌ Codex 登录失败:{detail}\n\n发送 /login 重试。",
    "auth.codex.waiting": "🔐 Codex 仍在等待浏览器确认。请使用上面的链接和代码。",
    "auth.codex.required": "🔐 请先通过上面的链接授权 Codex,然后重新创建会话。",
    "auth.codex.check_failed": (
        "❌ 无法检查 Codex 授权。请检查 `codex --version` 和 "
        "`CODEX_COMMAND`,然后发送 /login。"
    ),
    "auth.codex.storage_mismatch": (
        "⚠ Codex 找到了 `auth.json`,但当前凭据存储不会读取它。请设置 "
        '`cli_auth_credentials_store = "file"`;机器人不会替换现有授权。'
    ),
    "btn.confirm": "✓ 确认",
    "btn.no": "× 否",
    "btn.yes_kill": "⚠ 是，终止",
    "btn.yes_delete": "⚠ 是，删除",
    "btn.yes_clear": "⚠ 是，清空",
    "btn.refresh": "🔄 刷新",
    "btn.save": "已保存",
    "btn.cancelled": "已取消",
    # Archive buttons
    "btn.restore": "⤴ 恢复",
    "btn.restore_with_name": "⤴ 恢复 {name}",
    "btn.inspect": "🔍 查看",
    "btn.open_session": "📜 {name}",
    "btn.delete": "🗑 删除",
    "btn.to_14d": "→ 14天",
    "btn.to_72h": "→ 72时",
    "mm.sessions": "📋 会话",
    "mm.status": "📊 状态",
    "mm.history": "📜 历史",
    "mm.shot": "🧑‍💻 截图",
    "mm.new": "🆕 新建",
    "mm.archive": "🗄 归档",
    "mm.settings": "⚙ 设置",
    "menu.title": "*菜单*",
    "menu.empty": "*菜单*\n\n无活动会话——从切换器选一个或点 🆕 新建。",
    "menu.active": "*菜单* · 活动: *{name}*",
    "settings.title": "*设置*",
    "settings.body": (
        "*设置*\n\n"
        "代理: `{agent}`\n"
        "语言: `{language}`\n"
        "卡片延迟: `{live_lag}秒`\n"
        "语音: `{voice}`\n\n"
        "_点击分组进行更改。_"
    ),
    "settings.group.agent": "代理",
    "settings.group.language": "语言",
    "settings.group.live_lag": "卡片延迟",
    "settings.group.voice": "语音",
    "settings.lag.body": (
        "*实时预览延迟*\n\n"
        "实时卡片编辑的合并窗口。\n"
        "`0秒` = 每个事件都更新,数值越高越安静。"
    ),
    "settings.voice.body": (
        "*语音识别*\n\n"
        "语音消息使用的后端。\n"
        "• `auto` — macOS 用 Apple, 其他用 whisper.cpp\n"
        "• `whisper` — 强制 whisper.cpp\n"
        "• `apple` — 强制 Apple Speech (仅 macOS)\n"
        "• `off` — 忽略语音"
    ),
    "settings.agent.body": (
        "*代理*\n\n"
        "整个机器人的全局后端。所有新会话统一使用 *Claude* 或 *Codex*。\n\n"
        "当前后端仍有活动会话时不能切换；请先结束或归档这些会话。"
    ),
    "settings.lang.body": "*语言*\n\n界面语言。切换除 Claude 自身输出外的一切文本。",
    "list.empty": "没有活动会话。点 🆕 新建以创建。",
    "conf.kill": (
        "终止 *{name}*?\nTmux 窗口结束,claude session id 已保存。\n可通过归档列表恢复。"
    ),
    "conf.done": "标记 *{name}* 为完成?\n目标已关闭,会话已归档。",
    "conf.delete": "从归档中删除 *{name}*?\n状态记录消失。JSONL 保留在磁盘。",
    "conf.clear": (
        "清空 *{name}*?\n先发送 Esc,然后 /clear。会话上下文将被擦除,"
        "无法恢复(不同于 Kill → Restore)。"
    ),
    "conf.killed": "💀 已终止 `{name}`",
    "conf.done_ok": "🎉 `{name}` 已标记完成。",
    "conf.deleted": "🗑 归档记录已删除。",
    "dir.title": "*选择工作目录*",
    "dir.current": "当前: `{path}`",
    "dir.empty": "_(无子目录)_",
    "dir.hint": "点文件夹进入,或选择当前目录",
    "dir.btn.up": "..",
    "dir.btn.select": "选择",
    "picker.title": "*恢复会话?*",
    "picker.summary": "第 {page}/{pages} 页 — 此目录共 {total} 个会话。",
    "picker.btn.start_fresh": "🆕 从零开始",
    "picker.btn.back_to_dirs": "← 返回目录",
    "toast.no_session": "无活动会话",
    "toast.window_gone": "窗口已消失",
    "toast.esc_sent": "⎋ 已发送 Esc",
    "toast.cleared": "🧹 上下文已清空",
    "toast.killed": "已终止",
    "toast.done": "已完成",
    "toast.deleted": "已删除",
    "toast.saved": "已保存",
    "toast.agent_live": "切换全局代理前，请先结束或归档所有活动会话。",
    "toast.restored": "已恢复",
    "toast.already_gone": "已不存在",
    "toast.nothing_to_kill": "没什么可终止的",
    "toast.term_opened": "🖥 已打开终端",
    "toast.invalid_page": "页面无效",
    "toast.session_not_found": "未找到会话",
    "toast.restore_failed": "恢复失败:{msg}",
    "toast.range_14d": "→ 14天",
    "toast.range_72h": "→ 72时",
    # Archive screen
    "archive.title": "已归档会话",
    "archive.range_72h": "(0–72时)",
    "archive.range_14d": "(0–14天)",
    "archive.empty": "此范围内没有已归档会话。",
    "archive.page_line": "第 {page}/{pages} 页 — 共 {total}",
    "archive.age.s": "{n}秒前",
    "archive.age.m": "{n}分前",
    "archive.age.h": "{n}时前",
    "archive.age.d": "{n}天前",
    "usage.title": "*Claude Code*",
    "usage.title.codex": "*OpenAI Codex*",
    "usage.unavailable": "实时使用数据不可用。",
    "usage.auth_required": "加载 Usage 需要 Codex 授权。请完成上方登录，然后刷新此页面。",
    "usage.5h": "5小时",
    "usage.week": "本周",
    "usage.week_sonnet": "本周 (Sonnet)",
    "usage.not_reported": "Codex 未报告",
    "usage.today": "今天",
    "usage.today_left": "还可用",
    "usage.today_overspent": "超出",
    "usage.used": "已使用",
    "usage.reset": "重置",
    "usage.extra": "Extra",
    "usage.on": "开",
    "usage.off": "关",
    "usage.fetching": "正在获取使用情况…",
    "settings.group.weekly_reset_day": "周重置",
    "settings.weeklyday.body": (
        "*每周重置日*\n\n"
        "Anthropic 周配额重置的星期。\n"
        "用于计算 weekly 行的 %/天 消耗速率。"
    ),
    "day.mon": "一",
    "day.tue": "二",
    "day.wed": "三",
    "day.thu": "四",
    "day.fri": "五",
    "day.sat": "六",
    "day.sun": "日",
    "settings.group.auto_approve": "自动同意",
    "settings.approve.body": (
        "*自动同意*\n\n"
        "对 --dangerously-skip-permissions 未覆盖的\n"
        "Claude Code 交互式 Yes/No 提示的处理方式\n"
        "(例如 WebFetch 域名信任):\n"
        "• `off` — 推送到聊天,手动点击\n"
        "• `on` — 所有提示自动 Yes"
    ),
    "approve.off": "关",
    "approve.on": "开",
    "settings.group.session_idle_hours": "自动归档时间",
    "settings.idle_archive.body": (
        "*会话自动归档*\n\n"
        "实时会话无活动达到所选小时数后自动归档。"
        "归档会话仍可通过菜单 → 归档恢复。"
    ),
    "settings.value.hours": "{value}小时",
    # Local terminal — 3-state (off / manual / auto).
    "local.off": "关",
    "local.manual": "按钮",
    "local.auto": "总是",
    "settings.group.card_history": "卡片历史",
    "settings.cardhist.body": (
        "*卡片历史*\n\n"
        "首次访问时(机器人重启 / 切换器点击 / 菜单→Sessions)\n"
        "从 JSONL 转录加载多少最近的 end-of-turn 边界。\n"
        "更深的历史始终通过 /history 访问,与该值无关。\n\n"
        "更多 = 卡片内更多历史,每会话占用更多内存。"
    ),
    "settings.group.card_page_lines": "页面大小",
    "settings.pagesize.body": (
        "*页面大小*\n\n"
        "卡片单页最大行数。较旧事件落到前面的页面(◀);\n"
        "较长的最终回答按智能边界(段落 / 行 / 句子 / 单词)\n"
        "拆分多页 — 不会在单词中间断开。允许 ±5 行偏差。\n\n"
        "更小 = 手机视图更紧凑。更大 = 单页更多上下文,\n"
        "但 edit 消息更重。"
    ),
    "settings.group.card_inline_screenshots": "卡片内嵌截图",
    "settings.screens.body": (
        "*卡片内嵌截图*\n\n"
        "*开启* 时，终端 pane 截图会作为活动 Rich Markdown 卡片的\n"
        "最后一个媒体块，位于回复、工具状态和折叠内容之后。文本和\n"
        "截图持续在同一张 live 卡片中更新；你的下一条消息之后会在\n"
        "下方出现一张新卡片。仅当 pane 变化时刷新，约 3 秒节流。\n\n"
        "*关闭* 时，保持普通纯文本流程，Shot 可从顶部终端按钮打开。"
    ),
    "screens.on": "开",
    "screens.off": "关",
    "settings.group.bg_notify_finished": "Bg:任务完成",
    "settings.group.bg_notify_error": "Bg:错误",
    "settings.group.bg_notify_needs_action": "Bg:需要操作",
    "settings.bg_notify.finished.body": (
        "*Bg 会话:任务完成*\n\n"
        "后台会话进入 end-of-turn 时,推送 ✅ [<name>] task complete。"
    ),
    "settings.bg_notify.error.body": (
        "*Bg 会话:错误*\n\n后台会话发出错误事件时,推送 ❌ [<name>] error。"
    ),
    "settings.bg_notify.needs_action.body": (
        "*Bg 会话:需要操作*\n\n"
        "后台会话显示 AskUserQuestion / ExitPlanMode / Permission\n"
        "提示时,推送 ❓ [<name>] needs your attention。"
    ),
    "settings.cat.card": "🃏 卡片 / 视图",
    "settings.cat.notifications": "🔔 通知",
    "settings.cat.voice": "🎙 语音",
    "settings.cat.terminal": "🖥 本地终端",
    "settings.cat.behavior": "⚙ 代理、行为和语言",
    "settings.cat.card.body": "*卡片 / 视图*\n\n实时会话卡片的布局、密度和刷新。",
    "settings.cat.notifications.body": (
        "*通知*\n\nBg 会话推送(完成 / 错误 / 需要操作)和\nweekly quota 提醒的重置日。"
    ),
    "settings.cat.voice.body": "*语音*\n\n语音消息的 STT 后端。",
    "settings.cat.terminal.body": "*本地终端*\n\n附加到每个新会话的本地终端窗口。",
    "settings.cat.behavior.body": (
        "*行为和语言*\n\n全局代理；自动同意交互提示；界面语言。"
    ),
    "settings.group.local_terminal": "本地终端",
    "settings.local.body": (
        "*本地终端*\n\n"
        "可选的本地终端,附加到会话的 tmux 窗口 ——\n"
        "便于在桌面手动操作 Claude,与 Telegram 并行。\n\n"
        "*关* — 从不打开,不显示按钮。\n"
        "*按钮* — 不自动打开;当活动会话未附加终端时,\n"
        "*🖥 终端* 出现在 *停止 / 终止 / 清空 / 菜单* 旁边。\n"
        "*总是* — 每个新会话都自动打开,同时在未附加\n"
        "终端时显示相同的 *🖥 终端* 按钮。\n\n"
        "macOS:Terminal.app 或 iTerm2(自动)。\n"
        "Linux:在下方选择终端模拟器。如果自动检测\n"
        "不符合实际环境,请点击 *Configure via Claude*。"
    ),
    "settings.local.claude_help": "🪄 通过 Claude 配置",
    "help.home.body": (
        "*帮助*\n\n"
        "ccbot 将这个私聊连接到 N 个并行运行在 tmux 中的\n"
        "Claude Code 会话。点击下方对应章节查看简介。"
    ),
    "help.btn.overview": "概览",
    "help.btn.sessions": "会话",
    "help.btn.menu": "菜单",
    "help.btn.commands": "命令",
    "help.btn.voice": "语音和文件",
    "help.btn.alerts": "提醒",
    "help.btn.terminal": "本地终端",
    "help.btn.tips": "技巧",
    "help.body.overview": (
        "*概览*\n\n"
        "一个私聊,多个并行的 Claude Code 会话。任何文本会发送到\n"
        "当前的 *活动* 会话。每个会话拥有独立的 tmux 窗口和 claude\n"
        "进程,切换活动会话不会暂停其他会话。\n\n"
        "最新机器人消息下方的内联键盘是会话切换器和 ≡ 菜单。"
    ),
    "help.body.sessions": (
        "*会话*\n\n"
        "• *创建。* 在空 DM 中发送任意文本,或 ≡ 菜单 → 🆕 New,\n"
        "选择一个目录。\n"
        "• *切换。* 点击切换器中的会话按钮。\n"
        "• *引用回复。* 回复非活动会话的机器人消息 — 你的文本\n"
        "只单次路由到该会话,不更改活动状态。\n"
        "• *完成。* `/done [name]` — 标记并归档。\n"
        "• *闲置 TTL。* 无活动达到所选 6/12/24 小时后自动归档。\n"
        "• *恢复。* ≡ 菜单 → 📦 Archive → *Restore*。"
    ),
    "help.body.menu": (
        "*≡ 菜单*\n\n"
        "通过 /menu 或 ≡ 菜单内联按钮打开:\n"
        "• 📋 *Sessions* — 跳转到当前会话的实时卡片\n"
        "• 📊 *Status* — 5h / 周 / sonnet 配额\n"
        "• 🧑‍💻 *Shot* — 当前会话的终端快照\n"
        "• 🆕 *New* — 通过目录浏览器创建会话\n"
        "• 📦 *Archive* — 恢复 / 查看 / 删除\n"
        "• ⚙ *Settings* — 按 卡片 / 通知 / 语音 / 终端 / 行为 分组。"
    ),
    "help.body.commands": (
        "*斜杠命令*\n\n"
        "Bot 端:\n"
        "• `/menu` — 打开内联菜单\n"
        "• `/help` — 本帮助\n"
        "• `/done [name]` — 归档会话\n"
        "• `/health` — 运行时间 / 队列 / 延迟 / 计数器\n\n"
        "Claude Code 透传(原样转发):\n"
        "• `/model` `/effort` `/clear` `/compact` `/cost` `/memory` …\n\n"
        "前缀 `!` — 捕获本地 shell 命令的输出并转发。"
    ),
    "help.body.voice": (
        "*语音和文件*\n\n"
        "• *语音。* 发送语音消息 — 在本地转写\n"
        "(whisper.cpp / Apple Speech)然后作为文本发送给活动会话。\n"
        "• *照片 / 文档。* 落到 `<workdir>/.ccbot-inbox/`,Claude 收到\n"
        "相对路径(如果你附带 caption,会作为前缀)。TTL 24 小时;\n"
        "Telegram `file_id` 保留 30 天用于 `/restore-file`。"
    ),
    "help.body.alerts": (
        "*提醒*\n\n"
        "*配额提醒。* 5h / 周 / 周-Sonnet 配额 — 机器人每 10 分钟轮询\n"
        "实时 `/usage` 弹窗,百分比跨过 50 / 75 / 90 时推送。\n\n"
        "*后台会话推送。* Settings → 通知 三个独立开关(默认全部 on):\n"
        "• ✅ task complete\n"
        "• ❌ error\n"
        "• ❓ needs your attention (交互式提示)\n"
        "活动会话不推送 — 直接更新它的实时卡片。\n\n"
        "*上下文占用。* 卡片每个会话显示 ``context: N%``。Codex 使用\n"
        "rollout 中准确的 token usage 和模型窗口;Claude 使用 JSONL\n"
        "估算(最近一次 assistant turn 的 input + cache_read 除以模型窗口)。"
    ),
    "help.body.terminal": (
        "*本地终端*\n\n"
        "Settings → Local terminal:开启后,每次新建会话也会弹出\n"
        "本地原生窗口,自动 attach 到对应 tmux 窗口 —\n"
        "桌面手动操作和 Telegram 并行。\n\n"
        "macOS:Terminal.app / iTerm2(自动,iTerm 优先用 tab)。\n"
        "Linux:从自动检测列表选择,或 *Configure via Claude*\n"
        "处理特殊环境。\n\n"
        "随时也可直接 `tmux attach -t ccbot`。"
    ),
    "help.body.tips": (
        "*技巧*\n\n"
        "• *自动同意。* Settings → Auto-approve 自动 Yes\n"
        "--dangerously-skip-permissions 未覆盖的提示。\n"
        "• *Live lag。* Settings → Live lag — 会话卡片重绘频率,\n"
        "更小 = 更灵敏,更大 = 更省 rate-limit。\n"
        "• *语言。* Settings → Language:en / ru / zh。\n"
        "• *出站代理。* `TG_PROXY_URL` 如果主机无法\n"
        "直接访问 api.telegram.org。\n"
        "• *单实例锁。* bot 在 `$CCBOT_DIR/ccbot.lock` 持独占 flock;\n"
        "第二个 `uv run ccbot` 会拒绝启动并在 stderr 报错,\n"
        "不会和原实例争抢 Telegram updates。\n"
        "• *Hook 自愈。* `SessionStart` + `UserPromptSubmit` 都会更新\n"
        "`session_map.json` — 错过的 SessionStart 在下一个 prompt 自动修复。"
    ),
}

__all__ = ["ZH"]
