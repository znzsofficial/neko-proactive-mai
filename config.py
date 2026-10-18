"""Neko Proactive Mai 配置模型。"""

from __future__ import annotations

from typing import List

from maibot_sdk import Field, PluginConfigBase


GROUP_PROACTIVE_INTENT = "群聊冷场了，主动找个话题聊聊"


PRIVATE_PROACTIVE_INTENT = (
    "想找对方聊两句，可以分享自己的日常、感受或小计划，"
    "也可以顺着前面确实聊过、还没结束的事，问一句后来怎么样了。"
    "早晨可以照常说早安、问醒了没，或按约定叫起床。"
    "结合时间和最近对话选一个自然的话头，不机械查岗、不重复追问已回答的事，不必以问题收尾。"
    "已经在聊就顺着接，没合适的话想说时可以暂时不发。"
)


class PluginSection(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="2.0.0", description="配置文件版本号")
    version: str = Field(default="2.1.0", description="插件版本")


class SchedulerSection(PluginConfigBase):
    """调度器设置——控制何时、多久触发一次主动注入。"""

    enabled: bool = Field(default=True, description="是否开启主动聊天")
    start_time: str = Field(default="08:00", description="每日开始时间 (HH:MM)")
    end_time: str = Field(default="23:00", description="每日结束时间 (HH:MM)")
    check_interval: int = Field(default=30, description="检查间隔（分钟）")
    jitter: int = Field(default=5, description="间隔波动（分钟）")
    probability: float = Field(default=0.2, description="触发概率 0~1")
    min_interval_between_chats: int = Field(
        default=60, description="两次主动聊天最小间隔（分钟）"
    )
    isolation_time: int = Field(
        default=15, description="距上次真人消息至少静默多少分钟才触发"
    )


class TargetSection(PluginConfigBase):
    """白名单——只对指定的群/用户生效。"""

    allowed_groups: List[str] = Field(
        default_factory=list, description="白名单群号列表"
    )
    allowed_friends: List[str] = Field(
        default_factory=list, description="白名单QQ号列表"
    )


class LogSection(PluginConfigBase):
    verbose: bool = Field(default=True)
    scheduler_log: bool = Field(default=True)


class IntentSection(PluginConfigBase):
    """主动触发时注入 Planner 的意图描述。"""

    group_text: str = Field(
        default=GROUP_PROACTIVE_INTENT,
        description="群聊主动发言意图，默认普通冷场找话题；空白时也使用群聊默认，不沿用私聊意图",
    )
    private_text: str = Field(
        default=PRIVATE_PROACTIVE_INTENT,
        description="私聊主动发言意图：日常分享、旧事追问、早安/叫起床；空白时使用私聊默认",
    )
    reason: str = Field(
        default="attention_proactive", description="触发原因标识，用于日志和去重"
    )


class LifeSection(PluginConfigBase):
    enabled: bool = Field(
        default=False, description="通过签名接口获取Life公开见闻；失败仍按原流程聊天"
    )


class LonelyMaiConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    scheduler: SchedulerSection = Field(default_factory=SchedulerSection)
    target: TargetSection = Field(default_factory=TargetSection)
    log: LogSection = Field(default_factory=LogSection)
    intent: IntentSection = Field(default_factory=IntentSection)
    life: LifeSection = Field(default_factory=LifeSection)
