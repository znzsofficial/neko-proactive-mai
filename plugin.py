"""Neko Proactive Mai — 基于 Maisaka Planner 主动找话题。

不再自行构建 prompt 和调用 LLM，而是通过 maisaka.proactive.trigger()
将群聊冷场或私聊生活分享的独立意图注入 Planner 上下文，由 Maisaka 基于
人格、记忆、上下文和工具自行决定是否回复及如何表达。
"""

from __future__ import annotations

import asyncio
import random
import time
from datetime import datetime, time as datetime_time
from typing import Dict, Optional, Tuple

from maibot_sdk import Command, HookHandler, MaiBotPlugin
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .config import GROUP_PROACTIVE_INTENT, PRIVATE_PROACTIVE_INTENT, LonelyMaiConfig
from .prompt_visibility import filter_request, pending_offers, inject_offers


# ==================== 工具函数 ====================


def parse_time(time_str: str) -> datetime_time:
    hour, minute = map(int, time_str.split(":"))
    return datetime_time(hour, minute)


def is_in_time_range(current: datetime_time, start: datetime_time, end: datetime_time) -> bool:
    if start <= end:
        return start <= current <= end
    else:  # 跨夜
        return current >= start or current <= end


# ==================== 聊天流状态 ====================


class ChatStreamState:
    def __init__(self, stream_id: str, target_id: str, *, is_group: bool = False):
        self.stream_id = stream_id
        self.target_id = target_id
        self.is_group = is_group
        self.last_proactive_time: float = 0.0


# ==================== 插件主类 ====================


class LonelyMaiPlugin(MaiBotPlugin):
    config_model = LonelyMaiConfig

    def __init__(self):
        super().__init__()
        self._states: Dict[Tuple[bool, str], ChatStreamState] = {}
        self._scheduler_task: Optional[asyncio.Task] = None
        self._bot_qq: str = ""
        self._lifecycle_lock = asyncio.Lock()
        self._life = None

    # ========== 生命周期 ==========

    async def on_load(self) -> None:
        await self._sync_scheduler()

    async def on_unload(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
        self.ctx.logger.info("Neko Proactive Mai 已卸载")

    async def on_config_update(self, *args, **kwargs):
        del args, kwargs
        await self._sync_scheduler()

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _sync_scheduler(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
            if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                self.ctx.logger.info("Neko Proactive Mai 调度器已禁用")
                return
            self._bot_qq = await self._get_global_str("bot.qq_account", "")
            # Keep cooldown state; only the scheduler resolves targets, avoiding
            # concurrent replacement of states during an in-flight trigger.
            self._scheduler_task = asyncio.create_task(self._schedule_loop())
            self.ctx.logger.info("Neko Proactive Mai 调度器已启动")

    async def _get_global_str(self, key: str, default: str = "") -> str:
        try:
            val = await self.ctx.config.get(key, default)
        except Exception as e:
            self.ctx.logger.warning(f"读取全局配置 {key} 失败: {e}")
            return default
        if isinstance(val, dict):
            val = val.get("value", default)
        return str(val or default)

    async def _init_target_streams(self) -> None:
        previous = {key: state.last_proactive_time for key, state in self._states.items()}
        resolved: Dict[Tuple[bool, str], ChatStreamState] = {}

        for group_qq in self.config.target.allowed_groups:
            target_id = str(group_qq).strip()
            if not target_id:
                continue
            sid = await self._resolve_session_id(target_id, True)
            if sid:
                key = (True, target_id)
                state = ChatStreamState(sid, target_id, is_group=True)
                state.last_proactive_time = previous.get(key, 0.0)
                resolved[key] = state
            else:
                self.ctx.logger.warning(f"无法解析群聊: {target_id}")

        for user_qq in self.config.target.allowed_friends:
            target_id = str(user_qq).strip()
            if not target_id:
                continue
            sid = await self._resolve_session_id(target_id, False)
            if sid:
                key = (False, target_id)
                state = ChatStreamState(sid, target_id, is_group=False)
                state.last_proactive_time = previous.get(key, 0.0)
                resolved[key] = state
            else:
                self.ctx.logger.warning(f"无法解析私聊: {target_id}")

        changed = set(resolved) != set(self._states)
        self._states = resolved
        if changed or not resolved:
            self.ctx.logger.info(f"共注册 {len(self._states)} 个目标流")

    async def _resolve_session_id(self, qq: str, is_group: bool) -> Optional[str]:
        try:
            if is_group:
                result = await self.ctx.chat.get_stream_by_group_id(qq)
            else:
                result = await self.ctx.chat.get_stream_by_user_id(qq)
        except Exception as e:
            self.ctx.logger.error(f"ctx.chat 查询失败 ({qq}): {e}")
            return None

        if not isinstance(result, dict):
            self.ctx.logger.warning(f"会话查询返回了非字典 ({qq}): {type(result).__name__}")
            return None
        if result.get("success") is False:
            self.ctx.logger.warning(f"会话查询失败 ({qq}): {result.get('error') or result}")
            return None
        stream = result.get("stream", result if "session_id" in result else None)
        if not stream:
            return None
        if isinstance(stream, dict):
            return stream.get("session_id") or stream.get("stream_id")
        return getattr(stream, "session_id", None) or getattr(stream, "stream_id", None)

    # ========== 调度循环 ==========

    def _next_sleep_seconds(self) -> int:
        base_interval = max(1, self.config.scheduler.check_interval)
        jitter = self.config.scheduler.jitter
        actual_minutes = (
            base_interval + random.randint(-jitter, jitter) if jitter > 0 else base_interval
        )
        return max(60, actual_minutes * 60)

    async def _schedule_loop(self) -> None:
        while True:
            try:
                if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                    await asyncio.sleep(60)
                    continue

                await self._init_target_streams()
                if not self._states:
                    self.ctx.logger.warning("没有可用目标流，15 秒后重新解析")
                    await asyncio.sleep(15)
                    continue

                for state in list(self._states.values()):
                    target_id = state.target_id
                    allowed = (
                        self.config.target.allowed_groups
                        if state.is_group
                        else self.config.target.allowed_friends
                    )
                    if target_id not in allowed:
                        continue

                    try:
                        await self._check_and_trigger(state, target_id)
                    except Exception as e:
                        self.ctx.logger.error(f"[{target_id}] 检查异常: {e}", exc_info=True)

                sleep_seconds = self._next_sleep_seconds()
                if self.config.log.scheduler_log:
                    self.ctx.logger.info(f"[调度器] 下次检查: +{sleep_seconds // 60}min")
                await asyncio.sleep(sleep_seconds)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.ctx.logger.error(f"调度器异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_and_trigger(self, state: ChatStreamState, target_id: str) -> None:
        cfg = self.config
        now = datetime.now()
        current_time = now.time()
        start = parse_time(cfg.scheduler.start_time)
        end = parse_time(cfg.scheduler.end_time)

        if not is_in_time_range(current_time, start, end):
            self._skip(target_id, f"不在 {cfg.scheduler.start_time}-{cfg.scheduler.end_time}")
            return

        min_interval = cfg.scheduler.min_interval_between_chats * 60
        if (
            state.last_proactive_time
            and (now.timestamp() - state.last_proactive_time) < min_interval
        ):
            remain = int(min_interval - (now.timestamp() - state.last_proactive_time))
            self._skip(target_id, f"距上次主动还有 {remain} 秒")
            return

        isolation_seconds = cfg.scheduler.isolation_time * 60
        if isolation_seconds > 0:
            last_msg_time = await self._get_last_msg_time(state.stream_id)
            if last_msg_time is not None and (now.timestamp() - last_msg_time) < isolation_seconds:
                self._skip(target_id, "隔离期未到")
                return

        if random.random() >= cfg.scheduler.probability:
            self._skip(target_id, f"概率未命中 ({cfg.scheduler.probability})")
            return

        self.ctx.logger.info(f"[{target_id}] 触发主动聊天")
        if await self._do_proactive_chat(state, target_id):
            state.last_proactive_time = now.timestamp()

    # ========== 消息时间查询 ==========

    async def _get_last_msg_time(self, session_id: str) -> Optional[float]:
        """获取最近一条非 bot 消息的时间戳。"""
        try:
            end_ts = time.time()
            start_ts = end_ts - 3600 * 24 * 7
            raw = await self.ctx.message.get_by_time_in_chat(
                session_id,
                start_time=str(start_ts),
                end_time=str(end_ts),
                limit=5,
                limit_mode="latest",
            )
            if not isinstance(raw, list):
                return None

            for msg in raw:
                if not isinstance(msg, dict):
                    continue
                sender_id = self._get_msg_user_id(msg)
                if self._bot_qq and sender_id == self._bot_qq:
                    continue
                ts = msg.get("timestamp", 0)
                try:
                    return float(ts)
                except (TypeError, ValueError):
                    pass
            return None
        except Exception:
            return None

    # ========== 主动聊天核心 ==========

    @HookHandler(
        "maisaka.planner.before_request",
        name="lonelymai_latest_planner_task",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def filter_planner_tasks(self, **kwargs):
        modified, removed = filter_request(kwargs)
        items = modified.get("items")
        offers = []
        if isinstance(items, list) and self.config.plugin.enabled and self.config.life.enabled:
            try:
                from .life_bridge import LifeBridge

                if self._life is None:
                    self._life = LifeBridge(self.ctx)
                async with asyncio.timeout(2):
                    for record in pending_offers(kwargs):
                        state = next(
                            (s for s in self._states.values() if s.stream_id == record["scope"]),
                            None,
                        )
                        if state is None or not self._is_authorized(state, state.target_id):
                            continue
                        result = await self._life.call(
                            "prompt",
                            {
                                "session_id": record["scope"],
                                "receipt_id": record["receipt"],
                                "host_task": record["task"],
                            },
                        )
                        candidate = result.get("candidate")
                        if (
                            isinstance(candidate, dict)
                            and self._is_authorized(state, state.target_id)
                            and self.config.life.enabled
                        ):
                            offers.append(
                                (
                                    record["scope"],
                                    record["receipt"],
                                    {**candidate, "_intent": self._intent_for(state)},
                                )
                            )
            except Exception:
                pass  # Missing/expired state never resurrects an old candidate or sends anything.
        if offers:
            modified["items"] = inject_offers(items, offers)
        bili_offers = []
        if isinstance(items, list) and self.config.plugin.enabled and self.config.bilibili.enabled:
            try:
                from .bilibili_bridge import BilibiliBridge
                from .prompt_visibility import inject_bilibili_offers

                bridge = getattr(self, "_bilibili", None)
                if bridge is None:
                    bridge = self._bilibili = BilibiliBridge(self.ctx)
                async with asyncio.timeout(3):
                    for record in pending_offers(kwargs, source="bilibili"):
                        state = next(
                            (s for s in self._states.values() if s.stream_id == record["scope"]),
                            None,
                        )
                        if state is None or not self._is_authorized(state, state.target_id):
                            continue
                        result = await bridge.call(
                            "prompt",
                            {
                                "session_id": record["scope"],
                                "receipt_id": record["receipt"],
                                "host_task": record["task"],
                            },
                        )
                        if (
                            isinstance(result.get("candidate"), dict)
                            and self._is_authorized(state, state.target_id)
                            and self.config.bilibili.enabled
                        ):
                            bili_offers.append(
                                (record["scope"], record["receipt"], result["candidate"])
                            )
                if bili_offers:
                    modified["items"] = inject_bilibili_offers(
                        modified.get("items", items), bili_offers
                    )
            except Exception:
                pass  # Source outage never grants another chat or resurrects material.
        if removed or offers or bili_offers:
            return {"action": "continue", "modified_kwargs": modified}
        return None

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="lonelymai_latest_replyer_task",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
    )
    async def filter_replyer_tasks(self, **kwargs):
        return self._filter_proactive_request(kwargs)

    def _filter_proactive_request(self, kwargs):
        modified, removed = filter_request(kwargs)
        if not removed:
            return None
        self.ctx.logger.debug("本次请求过滤 %s 条旧主动任务，仅保留最新提示", removed)
        return {"action": "continue", "modified_kwargs": modified}

    def _intent_for(self, state: ChatStreamState) -> str:
        cfg = self.config.intent
        if state.is_group:
            return cfg.group_text.strip() or GROUP_PROACTIVE_INTENT
        return cfg.private_text.strip() or PRIVATE_PROACTIVE_INTENT

    def _is_authorized(self, state: ChatStreamState, target_id: str) -> bool:
        # Cached states preserve cooldowns, but are never an authorization source.
        allowed = (
            self.config.target.allowed_groups
            if state.is_group
            else self.config.target.allowed_friends
        )
        return (
            self.config.plugin.enabled
            and state.target_id == target_id
            and target_id in {str(value).strip() for value in allowed}
        )

    async def _do_proactive_chat(self, state: ChatStreamState, target_id: str) -> bool:
        """通过 Maisaka 主动触发 API 非显式注入意图到 Planner 上下文。

        群聊和私聊各选自己的意图，空白时使用各自默认。
        """
        if not self._is_authorized(state, target_id):
            self._skip(target_id, "插件已禁用或目标已撤权")
            return False
        candidate = None
        candidate_bridge = None
        reason = self.config.intent.reason
        metadata = {
            "source": "lonelymai",
            "target_id": target_id,
            "chat_kind": "group" if state.is_group else "private",
        }
        if self.config.life.enabled:
            try:
                from .life_bridge import LifeBridge

                if self._life is None:
                    self._life = LifeBridge(self.ctx)
                async with asyncio.timeout(3):
                    await self._life.flush()
                    candidate = await self._life.claim(state.stream_id)
                if candidate:
                    candidate_bridge = self._life
                    reason += '\n<neko_life_offer receipt_id="' + candidate["receipt_id"] + '"/>'
                    metadata["neko_life_receipt_id"] = candidate["receipt_id"]
            except Exception as exc:
                candidate = None
                self.ctx.logger.warning("Life候选获取或结算不可用: %s", type(exc).__name__)
        if self.config.bilibili.enabled:
            try:
                from .bilibili_bridge import BilibiliBridge

                bridge = getattr(self, "_bilibili", None)
                if bridge is None:
                    bridge = self._bilibili = BilibiliBridge(self.ctx)
                if candidate is None:
                    async with asyncio.timeout(3):
                        await bridge.flush()
                        candidate = await bridge.claim(state.stream_id)
                    if candidate:
                        candidate_bridge = bridge
                        reason += (
                            '\n<neko_bilibili_offer receipt_id="' + candidate["receipt_id"] + '"/>'
                        )
                        metadata["neko_bilibili_receipt_id"] = candidate["receipt_id"]
            except Exception as exc:
                self.ctx.logger.warning("B站阅读素材暂不可用: %s", type(exc).__name__)
        try:
            # Life I/O yields to config updates. Recheck immediately before Host I/O;
            # an already dispatched Host request cannot be recalled by this plugin.
            if not self._is_authorized(state, target_id):
                if candidate:
                    await candidate_bridge.update(candidate["receipt_id"], "released")
                return False
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=state.stream_id,
                intent=self._intent_for(state),
                reason=reason,
                metadata=metadata,
            )
            if candidate:
                task_id = (
                    result.get("task_id", "")
                    if isinstance(result, dict) and result.get("success") is True
                    else ""
                )
                if not isinstance(task_id, str) or not task_id.startswith(
                    "proactive:neko.proactive:"
                ):
                    task_id = ""
                try:
                    await candidate_bridge.update(
                        candidate["receipt_id"],
                        "submitted"
                        if task_id
                        else "released"
                        if isinstance(result, dict) and result.get("success") is False
                        else "unknown",
                        task_id,
                    )
                except Exception:
                    pass  # Durable lease/outbox prevents a second host trigger.
            if isinstance(result, dict) and result.get("success") is True:
                self.ctx.logger.info(f"[{target_id}] 已注入 Planner 上下文")
                return True
            outcome = (
                "宿主拒绝"
                if isinstance(result, dict) and result.get("success") is False
                else "宿主结果未知"
            )
            self.ctx.logger.warning(f"[{target_id}] 主动触发失败: {outcome}")
            return False
        except Exception as e:
            if candidate:
                try:
                    await candidate_bridge.update(candidate["receipt_id"], "unknown")
                except Exception:
                    pass
            self.ctx.logger.error(f"[{target_id}] 主动触发失败: {e}", exc_info=True)
            return False

    @HookHandler("send_service.after_send", name="lonelymai_life_receipt", mode=HookMode.OBSERVE)
    async def life_after_send(self, **kwargs):
        if not self.config.life.enabled:
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        try:
            if self._life is None:
                from .life_bridge import LifeBridge

                self._life = LifeBridge(self.ctx)
            await self._life.after_send(
                str(message.get("session_id") or ""),
                kwargs.get("reply_message_id"),
                kwargs.get("sent"),
            )
        except Exception:
            pass  # Pending durable receipt can be retried; never resend content.

    def _skip(self, target_id: str, reason: str) -> None:
        if self.config.log.scheduler_log:
            self.ctx.logger.info(f"[{target_id}] 跳过: {reason}")

    @staticmethod
    def _get_msg_user_id(msg: dict) -> str:
        info = msg.get("message_info") or {}
        user = info.get("user_info") or {}
        return str(user.get("user_id", "") or "")

    # ========== 手动命令 ==========

    @Command(
        "lonelymai_trigger",
        description="手动请求主动聊天，不保证发言；检查目标授权但不走自动时段/概率/冷却",
        pattern=r"^/saymai$",
    )
    async def cmd_trigger(self, stream_id: str = "", **kwargs):
        if not stream_id:
            return False, "无法获取当前会话 ID", True

        state = next((st for st in self._states.values() if st.stream_id == stream_id), None)
        if state is None:
            return False, "当前会话未在白名单中", True

        success = await self._do_proactive_chat(state, state.target_id)
        if success:
            return True, "已触发主动发言", True
        else:
            return False, "主动发言失败", True


def create_plugin():
    return LonelyMaiPlugin()
