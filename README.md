# Neko Proactive Mai

MaiBot 主动聊天调度插件，插件 ID 为 `neko.proactive`。

本项目是基于 [clfr41/neko.proactive](https://github.com/clfr41/neko.proactive)
的独立维护项目，保留原 AGPL-3.0-or-later 许可证和来源说明；不是上游仓库的官方项目。

通过Maisaka Planner主动发言：群聊默认普通冷场找话题，私聊可分享自己的日常、续聊旧事或问早。两种意图独立配置，仍使用现有人格、记忆、上下文，由白名单及原调度条件控制。

2.0.4：私聊默认提示精简，只保留主动分享日常、自然续聊旧事、早安/叫起床及避免机械重复的要求；身份、性格、表达风格和事实边界沿用宿主人设，不另写一遍。删除旧`intent.text`字段及回退逻辑；只设置group_text/private_text，空白分别使用各自默认。

2.0.5：Planner/Replyer请求发送前，本插件完整独立`plugin_proactive_task`提示仅保留最新一条（任务时间戳，同时间取末项）。不清历史/数据库、不修改调度或群私意图、不额外注入提示或触发聊天。仅识别本插件ID的纯文本UserMessageItem，用户引用/混合图片/助手及工具消息/其他插件不按关键词过滤；无法识别的结构保守保留，若有人完整仿造同一宿主任务格式则无法区分。过滤不改变已形成的模型回复或记忆，也不保证模型不再提插件。聚合Hook超过宿主16MiB时仍可能SKIP，未修改宿主限制。

2.1.0：新增`life.enabled=false`的可选Life公开见闻联动，仅在原调度规则通过后查询候选，
无候选/服务关闭/鉴权失败时仍走原普通主动聊天。群私意图、概率、目标、冷却及source=lonelymai保留，
不增加第二个定时器、不自行搜索或生成内容。可选查询含outbox收口最多等3秒。
使用独立0600的data_dir/life-client.json能力密钥，需与Life sharing-keys.json配对，不能仅凭调用参数中的插件ID授权。
候选预留和Host任务归因持久化到life-outbox.json；提交成功不等于发出，只有精确任务/会话成功回执才标sent。
回执锚点是普通QQ消息号、Planner沉默或RPC结果未知时不猜测已发送、不自动重发内容；
未知记录可能保守占用Life分享额度。回执重试只做结算，不触发新聊天。文件损坏保留，满128条未清记录则暂停新候选。
此桥按宿主1.3.9/SDK2.12隔离模拟测试，真实分段/语音归因仍需后续观察，默认不启用。

自动调度与/saymai都按解析出的会话类型选意图，不猜session_id前缀；群/私状态按(会话类型,目标ID)区分，同号不覆盖、不混冷却。时段、频率、概率、隔离期、reason仍共用现有设置，未增加第二套调度器。**这只是Planner意图，不是强制生成器或独立生活模拟器**：可能不发言，不保证每次完全遵循，也没有新增生活事件数据库/额外模型调用。

2.0.1：配置热更新统一管理调度器启停；从禁用状态启用即可开始调度，重复更新不会叠加任务，重新调度保留会话冷却时间。

## 与 v1.x 的区别

**v2.0.0 是一个彻底的重构。**

旧版（v1.x）自行构建 prompt → 直接调 LLM → 自己发消息，绕过了 Maisaka 的整个人格/记忆/表达系统。麦麦主动说话时跟被动回复时的性格不一样——等于两个人在用同一个号。

新版把"找话题"的意图注入 Planner 上下文，由 Maisaka 全权处理：
- 使用和被动回复**相同的人格和记忆**
- 自行判断是否应该回复、回复什么、怎么表达
- 不再需要单独配置 LLM 模型、prompt 模板、表达风格

## 安装

将本项目目录放到 MaiBot 的 `plugins/` 下并在 WebUI 启用。插件 ID 为 `neko.proactive`，首次迁移时由维护脚本迁移配置和Life能力文件。支持源码和配置热加载，不需要重启麦麦。

## 配置

```toml
[plugin]
enabled = false
config_version = "2.0.0"
version = "2.1.0"

[scheduler]
enabled = true
start_time = "08:00"        # 每日开始时间
end_time = "23:00"          # 每日结束时间
check_interval = 30          # 检查间隔（分钟）
jitter = 3                   # 间隔波动（分钟）
probability = 0.7            # 触发概率 0~1
min_interval_between_chats = 60  # 两次主动最小间隔（分钟）
isolation_time = 0           # 距上次真人消息至少静默分钟数

[target]
allowed_groups = []          # 白名单群号
allowed_friends = ["12345678"]  # 白名单QQ号

[log]
verbose = true
scheduler_log = true

[intent]
group_text = "群聊冷场了，主动找个话题聊聊" # 群聊专用，空白也回退普通冷场
private_text = "分享自己的日常，也可以接着聊旧事，早晨照常问早或叫起床" # 自定义例子，完整精简默认见config.py/随仓库模板
reason = "attention_proactive"         # 触发原因标识
```

在WebUI的intent设置里分别填写group_text和private_text；旧text已删除，不再读取或回退。源码发布仍不覆盖线上配置，更新现有私聊提示需保存private_text；清理旧字段可通过配置原始TOML编辑器删除intent下的text。

## 使用

插件按调度周期自动检查，满足条件时通过 `maisaka.proactive.trigger()` 注入意图。

手动触发：在已注册的会话中发送 `/saymai`。

### 触发流程

```
调度 tick → 时间范围检查 → 间隔检查 → 隔离期检查 → 概率判定
     ↓ 全部通过
maisaka.proactive.trigger(intent)
     ↓
Maisaka Planner 收到意图 → 基于人格/记忆/上下文/工具
     ↓
自行决定是否回复、回复什么、如何表达
```

## 手动触发

在已注册的白名单会话中发送 `/saymai`，立即触发同一份意图注入。当前实现绕过自动调度的时间范围、概率、最小间隔和隔离期；不绕过白名单，也不保证Planner一定发言。命令没有新增发送者管理员鉴权。

# 行为边界

## 一、改动过大

v2.0.0 删掉了 v1.x 的整套 prompt 构建、LLM 调用、消息格式化逻辑。如果你是从旧版升级，配置文件的 `[topic]`、`[llm]`、`[message]`、`[persona]`、`[prompt]` 节全部作废，需要删除或忽略。不删除也不会报错（toml 允许未知节），但不会再有任何效果。

## 二、与 Maisaka 行为绑定

v2.0.0 不再控制"说什么"，只控制"什么时候触发"。Maisaka 收到 intent 后可能：
- 回复一个话题（你想要的）
- 回一句"嗯"然后沉默
- 完全不理你（它觉得不需要说话）
- 调用其他工具（如果它觉得有必要）

这些行为本插件不控制。如果麦麦不说话，那是 Maisaka 的决定，不是插件坏了。

## 三、功能性免责

- 所有目标流注册在内存中，插件重载后重新查询 stream_id
- `isolation_time` 依赖 `message.get_by_time_in_chat` 查询历史消息，极端情况下可能查不到最新消息
- 全局配置 `bot.qq_account` 未设置时无法过滤 bot 自己的消息，隔离期可能被自己触发打断
- 白名单为空时不生效——这是设计，不是 bug

## 四、宇宙级免责

本插件按「AS IS」提供。作者不对以下后果负责，包括但不限于：
- 麦麦在群里突然开始聊奇怪的话题
- 麦麦因为 Maisaka 觉得"不想说话"而一直沉默
- 麦麦在凌晨三点因为时区配置错误触发了
- 群友发现麦麦的性格比原来活泼/高冷了一倍
- 猫踩键盘修改了 intent 文本

使用即接受。不接受就别开 `scheduler.enabled`。

# 来源与许可证

本项目保留上游 AGPL-3.0-or-later 许可证。原项目作者、版权和改动边界见
[NOTICE.md](NOTICE.md)。

# 遗留框架内

## v1.2.0 — 最后一代自行构建 prompt 的版本

v1.2.0 及之前的版本使用以下架构：

```
插件调度 → 构建 prompt（含历史消息/预置话题/知识库）
         → 直接调 llm.generate()
         → ctx.send.text() 发消息
```

### 保留的 v1.x 特性说明（仅作历史记录）

- **话题来源**：`history` / `knowledge` / `preset` / `mixed` 四种模式
- **Prompt 模板**：`[prompt]` 节自定义开场白，支持 `{nick}` `{persona}` 变量
- **表达风格**：`[persona]` 节额外指定说话风格
- **LLM 配置**：`[llm]` 节指定模型名称和最大 token
- **消息格式**：`[message]` 节控制时间前缀

这些配置在 v2.0.0 中全部移除，改用 `[intent]` 单条意图文本。

### 配置对比

| 功能 | v1.x | v2.0.0 |
|------|------|--------|
| 话题来源 | history/knowledge/preset/mixed + 权重 | 由 Maisaka 基于上下文自行决定 |
| 历史消息 | 插件自己查 `get_by_time_in_chat` 拼 prompt | Maisaka 自带上下文 |
| LLM 调用 | `ctx.llm.generate()` + 独立 prompt | 走 Maisaka Planner 管线 |
| 消息发送 | `ctx.send.text()` | Maisaka 自行决定是否回复 |
| 人格/风格 | 全局配置 + `speak_style` 叠加 | Maisaka 统一人格 |
| 意图控制 | 复杂的 prompt 模板系统 | 单条 intent 文本 |
| 配置体积 | ~30 个配置项 | ~15 个配置项 |

## 许可证

AGPL v3.0
