"""Neko Proactive Mai — 基于 Maisaka Planner 主动找话题。

不再自行构建 prompt 和调用 LLM，而是通过 maisaka.proactive.trigger()
将群聊冷场或私聊生活分享的独立意图注入 Planner 上下文，由 Maisaka 基于
人格、记忆、上下文和工具自行决定是否回复及如何表达。
"""

from __future__ import annotations

import asyncio
import random
import time
import json
from datetime import datetime, time as datetime_time
from typing import Dict, Optional, Tuple

from maibot_sdk import API, Command, HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo

from .config import GROUP_PROACTIVE_INTENT, PRIVATE_PROACTIVE_INTENT, LonelyMaiConfig
from .prompt_visibility import filter_request, pending_offers, inject_offers
from .wake_store import CONTRACT, PENDING, WakeStore, text
from .webui import WakeManagement


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


class LonelyMaiPlugin(WakeManagement, MaiBotPlugin):
    config_model = LonelyMaiConfig

    def __init__(self):
        super().__init__()
        self._states: Dict[Tuple[bool, str], ChatStreamState] = {}
        self._scheduler_task: Optional[asyncio.Task] = None
        self._bot_qq: str = ""
        self._lifecycle_lock = asyncio.Lock()
        self._life = None
        self._wake_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._wakes = None
        self._wake_ready = False
        self._next_regular_at = None
        self._next_pass_at = None
        self._scheduler_phase = "stopped"
        self._observations = {}
        self._submission_results = {}
        self._management_operation = "尚无管理操作"
        self._management_epoch = 0

    # ========== 生命周期 ==========

    async def on_load(self) -> None:
        self._get_wakes().recover(time.time())
        self._wake_ready = True
        await self._sync_scheduler()

    async def on_unload(self) -> None:
        self._management_epoch = getattr(self, "_management_epoch", 0) + 1
        self._wake_ready = False
        async with self._lifecycle_lock:
            await self._stop_scheduler()
        self.ctx.logger.info("Neko Proactive Mai 已卸载")

    async def on_config_update(self, *args, **kwargs):
        del args, kwargs
        self._management_epoch = getattr(self, "_management_epoch", 0) + 1
        await self._sync_scheduler()

    async def _stop_scheduler(self) -> None:
        task = self._scheduler_task
        self._scheduler_task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._next_regular_at = self._next_pass_at = None
        self._scheduler_phase = "stopped"

    async def _sync_scheduler(self) -> None:
        async with self._lifecycle_lock:
            await self._stop_scheduler()
            # Management stays readable when automatic scheduling is disabled.
            self._bot_qq = await self._get_global_str("bot.qq_account", "")
            if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                self.ctx.logger.info("Neko Proactive Mai 调度器已禁用")
                return
            # Keep cooldown state; only the scheduler resolves targets, avoiding
            # concurrent replacement of states during an in-flight trigger.
            self._scheduler_task = asyncio.create_task(self._schedule_loop())
            self._scheduler_phase = "starting"
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

    def _get_wakes(self):
        if self._wakes is None:
            self._wakes = WakeStore(self.ctx.paths.data_dir)
        return self._wakes

    def _automatic_enabled(self):
        return self.config.plugin.enabled and self.config.scheduler.enabled

    def _guard_until(self, row):
        return row["attempted"] + max(60, self.config.scheduler.min_interval_between_chats * 60)

    @Tool(
        "neko_proactive_wake",
        brief_description="管理当前授权私聊的下次自主唤醒：查看、预约/改期、取消；不是定点提醒。",
        detailed_description="set替换唯一预约，at为含UTC偏移的ISO8601，timezone为匹配的IANA时区，intent写简短意图/依据。60秒至7天内；到点仍检查时段/冷却，可不发言。",
        parameters=[
            ToolParameterInfo(
                name="action", description="get/set/cancel", enum_values=["get", "set", "cancel"]
            ),
            ToolParameterInfo(
                name="at", description="绝对时刻，如2026-11-01T09:00:00+08:00", required=False
            ),
            ToolParameterInfo(
                name="timezone", description="IANA时区，如Asia/Shanghai", required=False
            ),
            ToolParameterInfo(
                name="intent", description="本次唤醒的简短意图与依据，最多240字符", required=False
            ),
        ],
        platforms=["qq"],
        chat_scope="private",
        trusted_context=CONTRACT,
    )
    async def private_wake(self, action="get", at="", timezone="", intent="", **kwargs):
        try:
            async with self._wake_lock:
                host = kwargs.get("_host_context")
                if not self._wake_ready or not self._automatic_enabled():
                    raise ValueError("wake_disabled")
                if (
                    not isinstance(host, dict)
                    or host.get("contract") != CONTRACT
                    or host.get("platform") != "qq"
                    or host.get("group_id")
                    or not text(host.get("user_id"), 128)
                    or not text(host.get("session_id"), 128)
                    or host.get("stream_id") != host["session_id"]
                    or (host.get("account_id") and host["account_id"] != self._bot_qq)
                ):
                    raise ValueError("trusted_private_context_required")
                target, session = host["user_id"], host["session_id"]
                state = ChatStreamState(session, target)
                resolved = await self._resolve_session_id(target, False)
                if (
                    not self._wake_ready
                    or not self._automatic_enabled()
                    or not self._is_authorized(state, target)
                    or resolved != session
                ):
                    raise ValueError("private_chat_not_authorized")
                row = self._change_wake_locked(action, target, session, at, timezone, intent)
                return {"success": True, "wake": row}
        except (ValueError, OSError) as exc:
            return {
                "success": False,
                "error": str(exc) if isinstance(exc, ValueError) else "wake_storage_unavailable",
            }

    def _change_wake_locked(
        self, action, target, session, at="", zone="", intent="", *, revision=None, partial=False
    ):
        """Both trusted model and authenticated admin use this lock-held mutation."""
        from .webui import wake_revision

        if not self._wake_ready:
            raise ValueError("wake_disabled")
        if target not in {str(value).strip() for value in self.config.target.allowed_friends}:
            raise ValueError("private_chat_not_authorized")
        store = self._get_wakes()
        data = store.read()
        row = data["slots"].get(target)
        if revision is not None and revision != wake_revision(target, session, row):
            raise ValueError("stale_wake_revision")
        if row and row["session"] != session and action != "set":
            raise ValueError("session_changed")
        if action == "set":
            if not self._automatic_enabled():
                raise ValueError("wake_disabled")
            keep_time = (
                partial
                and row
                and row["status"] in PENDING
                and row["session"] == session
                and not at
            )
            if partial and row and row["status"] in PENDING and row["session"] == session:
                at, zone, intent = at or row["at"], zone or row["timezone"], intent or row["intent"]
            row = store.set(
                data, target, session, at, zone, intent, time.time(), keep_time=bool(keep_time)
            )
        elif action == "cancel":
            if row and row["status"] in PENDING:
                row.update(status="cancelled", block="")
                store.write(data)
        elif action != "get":
            raise ValueError("invalid_action")
        if action != "get":
            self._wake_event.set()
        return row

    @API("wake_overview", description="只读主动检查与私聊预约管理状态", public=False)
    async def wake_overview(self):
        return await self.management_snapshot()

    @API("wake_save", description="管理员设置或替换当前授权私聊预约", public=False)
    async def wake_save(self, target="", session="", revision="", at="", timezone="", intent=""):
        return await self.management_change("set", target, session, revision, at, timezone, intent)

    @API("wake_cancel", description="管理员取消当前授权私聊的待执行预约，不重放", public=False)
    async def wake_cancel(self, target="", session="", revision=""):
        return await self.management_change("cancel", target, session, revision)

    async def _run_wakes(self):
        async with self._wake_lock:
            if not self._wake_ready or not self._automatic_enabled():
                return
            store = self._get_wakes()
            data = store.read()
            dispatched = False
            for target, row in sorted(data["slots"].items(), key=lambda pair: pair[1]["due"]):
                now = time.time()
                if row["status"] not in PENDING:
                    continue
                state = self._states.get((False, target))
                if (
                    not state
                    or row["session"] != state.stream_id
                    or not self._is_authorized(state, target)
                ):
                    row.update(status="revoked", block="authorization_or_session_changed")
                    store.write(data)
                    continue
                if row["due"] > now or row["retry"] > now:
                    continue
                if now - row["due"] >= 86400:
                    row.update(status="expired", block="blocked_24h")
                    store.write(data)
                    continue
                queried_isolation = self.config.scheduler.isolation_time > 0
                last = await self._get_last_msg_time(state.stream_id) if queried_isolation else None
                self._note_check(state, "appointment", last_message=last)
                # Query session fresh; config/lifecycle may change during Host reads.
                resolved = await self._resolve_session_id(target, False)
                if not self._wake_ready or not self._automatic_enabled():
                    return
                if not self._is_authorized(state, target) or resolved != row["session"]:
                    row.update(status="revoked", block="authorization_or_session_changed")
                    store.write(data)
                    continue
                # All rule reads occur after async Host lookups, using the latest
                # config/time. A hot change must not open a stale admission window.
                cfg, now = self.config.scheduler, time.time()
                if now - row["due"] >= 86400:
                    row.update(status="expired", block="blocked_24h")
                    store.write(data)
                    continue
                block = ""
                if not is_in_time_range(
                    datetime.fromtimestamp(now).time(),
                    parse_time(cfg.start_time),
                    parse_time(cfg.end_time),
                ):
                    block = "activity_hours"
                elif now < max(
                    self._guard_until(row),
                    state.last_proactive_time + cfg.min_interval_between_chats * 60,
                ):
                    block = "cooldown"
                elif dispatched or now < data["last_dispatch"] + 60:
                    block = "dispatch_spacing"
                elif cfg.isolation_time > 0 and (
                    not queried_isolation
                    or (last is not None and now - last < cfg.isolation_time * 60)
                ):
                    block = "isolation"
                if block:
                    self._note_check(state, block, last_message=last)
                    row.update(status="blocked", block=block, retry=now + 60)
                    store.write(data)
                    continue
                # Commit the uncertainty boundary BEFORE any Host submission.
                # Cancellation, process loss, malformed results never replay this ID.
                row.update(
                    status="unknown",
                    last_outcome="unknown",
                    attempted=now,
                    block="",
                    task="",
                    shown=False,
                )
                data["last_dispatch"] = now
                store.write(data)
                dispatched = True
                self._record_submission(state, "unknown", "appointment", now)
                try:
                    result = await self.ctx.maisaka.proactive.trigger(
                        stream_id=state.stream_id,
                        intent=self._intent_for(state),
                        reason=self.config.intent.reason,
                        metadata={
                            "source": "lonelymai",
                            "target_id": target,
                            "chat_kind": "private",
                            "neko_wake_id": row["id"],
                        },
                    )
                    if isinstance(result, dict) and result.get("success") is True:
                        row["status"] = "submitted"
                        row["last_outcome"] = "submitted"
                        task = result.get("task_id")
                        if text(task, 160) and task.startswith("proactive:neko.proactive:"):
                            row["task"] = task
                        state.last_proactive_time = now
                    elif isinstance(result, dict) and result.get("success") is False:
                        row["status"] = "rejected"
                        row["last_outcome"] = "rejected"
                except Exception:
                    self.ctx.logger.warning("私聊预约提交结果未知；不自动重放")
                store.write(data)
                self._record_submission(state, row["status"], "appointment", now)

    def _wake_delay(self):
        now = time.time()
        rows = self._get_wakes().read()["slots"].values()
        return min(
            (max(1, max(r["due"], r["retry"]) - now) for r in rows if r["status"] in PENDING),
            default=86400,
        )

    def _next_sleep_seconds(self) -> int:
        base_interval = max(1, self.config.scheduler.check_interval)
        jitter = self.config.scheduler.jitter
        actual_minutes = (
            base_interval + random.randint(-jitter, jitter) if jitter > 0 else base_interval
        )
        return max(60, actual_minutes * 60)

    async def _schedule_loop(self) -> None:
        next_regular = 0
        while True:
            try:
                if not self.config.plugin.enabled or not self.config.scheduler.enabled:
                    await asyncio.sleep(60)
                    continue

                self._scheduler_phase = "resolving"
                self._next_pass_at = None
                self._wake_event.clear()
                await self._init_target_streams()
                if not self._states:
                    await self._run_wakes()
                    self._scheduler_phase = "unresolved"
                    self._next_pass_at = time.time() + 15
                    self.ctx.logger.warning("没有可用目标流，15 秒后重新解析")
                    await asyncio.sleep(15)
                    continue

                regular = time.monotonic() >= next_regular
                self._scheduler_phase = "checking"
                for state in list(self._states.values()) if regular else []:
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

                if regular:
                    next_regular = time.monotonic() + self._next_sleep_seconds()
                    self._next_regular_at = time.time() + max(0, next_regular - time.monotonic())
                await self._run_wakes()
                sleep_seconds = min(max(1, next_regular - time.monotonic()), self._wake_delay())
                self._scheduler_phase = "waiting"
                self._next_pass_at = time.time() + sleep_seconds
                if self.config.log.scheduler_log:
                    self.ctx.logger.info(f"[调度器] 下次检查: +{int(sleep_seconds // 60)}min")
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=sleep_seconds)
                except TimeoutError:
                    pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._scheduler_phase = "error"
                self._next_pass_at = time.time() + 60
                self.ctx.logger.error(f"调度器异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_and_trigger(self, state: ChatStreamState, target_id: str) -> None:
        if not state.is_group:
            async with self._wake_lock:
                row = self._get_wakes().read()["slots"].get(target_id)
                if row and (row["status"] in PENDING or time.time() < self._guard_until(row)):
                    self._note_check(
                        state, "appointment" if row["status"] in PENDING else "cooldown"
                    )
                    return
                await self._check_default_and_trigger(state, target_id)
            return
        await self._check_default_and_trigger(state, target_id)

    async def _check_default_and_trigger(self, state: ChatStreamState, target_id: str) -> None:
        cfg = self.config
        now = datetime.now()
        self._note_check(state, "checking")
        current_time = now.time()
        start = parse_time(cfg.scheduler.start_time)
        end = parse_time(cfg.scheduler.end_time)

        if not is_in_time_range(current_time, start, end):
            self._note_check(state, "activity_hours")
            self._skip(target_id, f"不在 {cfg.scheduler.start_time}-{cfg.scheduler.end_time}")
            return

        min_interval = cfg.scheduler.min_interval_between_chats * 60
        if (
            state.last_proactive_time
            and (now.timestamp() - state.last_proactive_time) < min_interval
        ):
            remain = int(min_interval - (now.timestamp() - state.last_proactive_time))
            self._note_check(state, "cooldown")
            self._skip(target_id, f"距上次主动还有 {remain} 秒")
            return

        isolation_seconds = cfg.scheduler.isolation_time * 60
        if isolation_seconds > 0:
            last_msg_time = await self._get_last_msg_time(state.stream_id)
            self._note_check(state, "checking", last_message=last_msg_time)
            if last_msg_time is not None and (now.timestamp() - last_msg_time) < isolation_seconds:
                self._skip(target_id, "隔离期未到")
                self._note_check(state, "isolation", last_message=last_msg_time)
                return

        if random.random() >= cfg.scheduler.probability:
            self._note_check(state, "probability")
            self._skip(target_id, f"概率未命中 ({cfg.scheduler.probability})")
            return

        self.ctx.logger.info(f"[{target_id}] 触发主动聊天")
        self._note_check(state, "eligible")
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
        wake_items = await self._wake_prompt(kwargs)
        if wake_items:
            modified["items"] = [*modified.get("items", []), *wake_items]
        if removed or offers or bili_offers or wake_items:
            return {"action": "continue", "modified_kwargs": modified}
        return None

    async def _wake_prompt(self, kwargs):
        from .prompt_visibility import EPHEMERAL, route_scopes, task_record
        from datetime import timezone

        if not self._wake_ready or not self._automatic_enabled():
            return []
        try:
            async with self._wake_lock:
                store = self._get_wakes()
                data, scopes = store.read(), route_scopes(kwargs)
                items = kwargs.get("items")
                if not isinstance(items, list):
                    return []
                for item in reversed(items):
                    record = task_record(item)
                    if not record or not record["wake"]:
                        continue
                    scope = record["scope"] or kwargs.get("session_id", "")
                    for target, row in data["slots"].items():
                        state = self._states.get((False, target))
                        if (
                            scope not in scopes
                            or row["session"] != scope
                            or row["id"] != record["wake"]
                            or row["status"] != "submitted"
                            or row["task"] != record["task"]
                            or row["shown"]
                            or not 0 <= time.time() - row["attempted"] < 180
                            or state is None
                            or state.stream_id != scope
                            or not self._is_authorized(state, target)
                        ):
                            continue
                        row["shown"] = True
                        store.write(data)
                        return [
                            {
                                "item_type": "SystemMessageItem",
                                "meta": {
                                    "item_id": EPHEMERAL + row["id"],
                                    "logical_turn_id": None,
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                },
                                "parts": [
                                    {
                                        "type": "text",
                                        "text": "本轮私聊自主预约意图（不是用户的新消息）："
                                        + json.dumps(
                                            {
                                                "session_id": scope,
                                                "at": row["at"],
                                                "timezone": row["timezone"],
                                                "intent": row["intent"],
                                            },
                                            ensure_ascii=False,
                                        )
                                        + "。结合最新上下文判断意图是否过时；可不发言，不承诺提醒已送达。",
                                    }
                                ],
                            }
                        ]
        except (ValueError, OSError):
            self.ctx.logger.warning("私聊预约临时上下文不可用")
        return []

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
                self._record_submission(state, "submitted", "default")
                self.ctx.logger.info(f"[{target_id}] 已注入 Planner 上下文")
                return True
            outcome = (
                "宿主拒绝"
                if isinstance(result, dict) and result.get("success") is False
                else "宿主结果未知"
            )
            self._record_submission(
                state,
                "rejected"
                if isinstance(result, dict) and result.get("success") is False
                else "unknown",
                "default",
            )
            self.ctx.logger.warning(f"[{target_id}] 主动触发失败: {outcome}")
            return False
        except Exception as e:
            self._record_submission(state, "unknown", "default")
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
