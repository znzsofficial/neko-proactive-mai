"""Bidirectional Host RPC ordering with real PluginContext, no provider/send calls."""

import asyncio
import copy
import json
import unittest
from unittest.mock import AsyncMock

import test_chat_sdk as fixtures
from test_wakes import NOW, iso
from test_prompt_visibility import item, task


class PlanDispatchTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.ChatSDKTests.setUp
    identity = fixtures.ChatSDKTests.identity
    selected = fixtures.ChatSDKTests.selected

    async def asyncSetUp(self):
        await fixtures.ChatSDKTests.asyncSetUp(self)
        await self.bot._init_target_streams()
        self.original_rpc = self.context._rpc_call
        self.trigger_calls = []
        self.hook_result = None

    async def create(self, repeat=False):
        row = await self.selected()
        fields = (
            {
                "kind": "interval",
                "interval_minutes": 5,
                "timezone": "UTC",
                "intent": "合成明确计划依据",
            }
            if repeat
            else {"at": iso(NOW + 300), "timezone": "UTC", "intent": "合成明确计划依据"}
        )
        result = await self.bot.wake_edit(
            "repeat_set" if repeat else "single_save", **self.identity(row), **fields
        )
        self.assertTrue(result["success"], result)

    def records(self):
        data = self.bot._get_wakes().read()
        return data, data.get("repeats", {}).get("friend", {}).get("last") or data["slots"].get(
            "friend"
        )

    async def test_planner_hook_during_real_sdk_rpc_before_ack_binds_purpose_once(self):
        for repeated in (False, True):
            with self.subTest(repeat=repeated):
                self.clock_mock.return_value = NOW
                self.bot._get_wakes().path.unlink(missing_ok=True)
                await self.create(repeated)
                cfg = self.bot.config.scheduler
                cfg.start_time = cfg.end_time = "00:00"
                cfg.probability = 0
                cfg.min_interval_between_chats = 60
                cfg.isolation_time = 60
                self.bot._states[(False, "friend")].last_proactive_time = NOW + 299
                self.bot._get_last_msg_time = AsyncMock(return_value=NOW + 299)

                async def rpc(method, plugin_id, payload, **kwargs):
                    if payload["capability"] != "maisaka.proactive.trigger":
                        return await self.original_rpc(method, plugin_id, payload, **kwargs)
                    args = payload["args"]
                    self.trigger_calls.append(copy.deepcopy(args))
                    self.assertFalse(self.bot._wake_lock.locked())
                    _, before = self.records()
                    self.assertEqual(before["status"], "unknown")
                    text = task(1793505900000).replace(
                        '{"source": "lonelymai"}', json.dumps(args["metadata"])
                    )
                    self.hook_result = await asyncio.wait_for(
                        self.bot.filter_planner_tasks(
                            session_id="session-friend", items=[item(text)]
                        ),
                        timeout=0.5,
                    )
                    self.assertIn(
                        "合成明确计划依据", json.dumps(self.hook_result, ensure_ascii=False)
                    )
                    _, observed = self.records()
                    self.assertEqual(observed["status"], "unknown")
                    self.assertTrue(observed["shown"])
                    self.assertEqual(observed["planner_requested_at"], NOW + 300)
                    self.assertEqual(
                        await self.bot._wake_prompt(
                            {"session_id": "session-friend", "items": [item(text)]}
                        ),
                        [],
                    )
                    return {
                        "success": True,
                        "task_id": "proactive:neko.proactive:1793505900000",
                        "queued": True,
                    }

                self.context._rpc_call = rpc
                self.clock_mock.return_value = NOW + 300
                await asyncio.wait_for(self.bot._run_wakes(), timeout=1)
                _, after = self.records()
                self.assertEqual(after["status"], "submitted")
                self.assertTrue(after["shown"])
                self.assertEqual(after["task"], "proactive:neko.proactive:1793505900000")
                self.assertEqual(self.bot._states[(False, "friend")].last_proactive_time, NOW + 299)
                self.bot._get_last_msg_time.assert_not_awaited()

    async def test_other_target_write_is_not_lost_while_rpc_awaits_and_same_target_edit_denied(
        self,
    ):
        await self.create()
        self.bot.config.target.allowed_friends.append("other")
        entered, release = asyncio.Event(), asyncio.Event()

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await self.original_rpc(method, plugin_id, payload, **kwargs)
            entered.set()
            await release.wait()
            return {"success": True, "task_id": "proactive:neko.proactive:1793505900000"}

        self.context._rpc_call = rpc
        self.clock_mock.return_value = NOW + 300
        active = asyncio.create_task(self.bot._run_wakes())
        await entered.wait()
        row = await self.selected()
        denied = await self.bot.wake_edit("single_cancel", **self.identity(row))
        self.assertFalse(denied["success"])
        self.assertIn("正在提交", denied["message"])
        async with self.bot._wake_lock:
            self.bot._change_wake_locked(
                "set", "other", "session-other", iso(NOW + 600), "UTC", "其他会话自己的预约"
            )
        await self.bot._run_wakes()  # concurrent pass cannot submit while in-flight
        release.set()
        await active
        data, row = self.records()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(data["slots"]["other"]["intent"], "其他会话自己的预约")

    async def test_queued_without_observed_hook_is_never_reported_as_planner_started(self):
        await self.create()

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await self.original_rpc(method, plugin_id, payload, **kwargs)
            return {
                "success": True,
                "queued": True,
                "task_id": "proactive:neko.proactive:1793505900000",
            }

        self.context._rpc_call = rpc
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        selected = await self.selected()
        self.assertIn("尚未观测", selected["planner_state"])
        self.assertNotIn("已执行", selected["planner_state"])
        self.assertFalse(self.records()[1]["shown"])

    async def test_unknown_hook_match_requires_exact_scope_id_and_preserves_no_replay(self):
        await self.create()
        self.clock_mock.return_value = NOW + 300

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await self.original_rpc(method, plugin_id, payload, **kwargs)
            args = payload["args"]
            text = task(1793505900000).replace(
                '{"source": "lonelymai"}', json.dumps(args["metadata"])
            )
            self.assertEqual(
                await self.bot._wake_prompt({"session_id": "different", "items": [item(text)]}), []
            )
            altered = text.replace(args["metadata"]["neko_wake_id"], "a" * 32)
            self.assertEqual(
                await self.bot._wake_prompt(
                    {"session_id": "session-friend", "items": [item(altered)]}
                ),
                [],
            )
            return None

        self.context._rpc_call = rpc
        await self.bot._run_wakes()
        raw = self.bot._get_wakes().path.read_bytes()
        self.bot._get_wakes().recover(NOW + 301)
        await self.bot._run_wakes()
        self.assertEqual(raw, self.bot._get_wakes().path.read_bytes())

    async def test_default_request_hook_does_not_wait_for_plan_lock(self):
        async with self.bot._wake_lock:
            result = await asyncio.wait_for(
                self.bot.filter_planner_tasks(
                    session_id="session-friend", items=[item("合成普通聊天")]
                ),
                timeout=0.25,
            )
            self.assertIsNone(result)

    async def test_fixed_sixty_second_target_safety_survives_replacement_and_restart(self):
        await self.create()

        async def rpc(method, plugin_id, payload, **kwargs):
            if payload["capability"] != "maisaka.proactive.trigger":
                return await self.original_rpc(method, plugin_id, payload, **kwargs)
            self.trigger_calls.append(payload["args"])
            return None

        self.context._rpc_call = rpc
        self.clock_mock.return_value = NOW + 300
        await self.bot._run_wakes()
        self.assertEqual(self.records()[1]["status"], "unknown")
        row = await self.selected()
        await self.bot.wake_edit(
            "single_save",
            **self.identity(row),
            at=iso(NOW + 360),
            timezone="UTC",
            intent="明确另建，不重试旧任务",
        )
        self.bot._wakes = None
        self.assertEqual(self.bot._target_guard(self.bot._get_wakes().read(), "friend"), NOW + 360)
        self.clock_mock.return_value = NOW + 359
        await self.bot._run_wakes()
        self.assertEqual(len(self.trigger_calls), 1)
        self.clock_mock.return_value = NOW + 360
        await self.bot._run_wakes()
        self.assertEqual(len(self.trigger_calls), 2)
