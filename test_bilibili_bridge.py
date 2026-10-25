import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from maibot_sdk.context import PluginContext
import test_intent as intents
from prompt_visibility import pending_offers, filter_request, inject_bilibili_offers, EPHEMERAL
import test_prompt_visibility as visibility


class BilibiliIntentTests(unittest.IsolatedAsyncioTestCase):
    setUp = intents.LifeIntentTests.setUp

    async def test_bilibili_uses_existing_trigger_without_source_body_in_history(self):
        self.bot.config.bilibili.enabled = True
        bridge = SimpleNamespace(
            flush=AsyncMock(),
            claim=AsyncMock(
                return_value={
                    "receipt_id": "a" * 32,
                    "protocol": 2,
                    "summary": "source must not enter history",
                }
            ),
            update=AsyncMock(),
        )
        self.bot._bilibili = bridge
        self.bot.ctx.maisaka.proactive.trigger.return_value = {
            "success": True,
            "task_id": "proactive:neko.proactive:1792930000000",
        }
        self.assertTrue(await self.bot._do_proactive_chat(self.state, "target"))
        args = self.bot.ctx.maisaka.proactive.trigger.await_args.kwargs
        self.assertIn("neko_bilibili_offer", args["reason"])
        self.assertNotIn("source must not enter history", str(args))
        self.assertEqual(args["intent"], self.bot.config.intent.private_text)
        self.bot.ctx.maisaka.proactive.trigger.assert_awaited_once()
        bridge.update.assert_awaited_once_with(
            "a" * 32, "submitted", "proactive:neko.proactive:1792930000000"
        )

    async def test_source_failure_falls_back_to_normal_chat_and_revoked_target_never_triggers(self):
        self.bot.config.bilibili.enabled = True
        self.bot._bilibili = SimpleNamespace(flush=AsyncMock(side_effect=ValueError("offline")))
        self.assertTrue(await self.bot._do_proactive_chat(self.state, "target"))
        args = self.bot.ctx.maisaka.proactive.trigger.await_args.kwargs
        self.assertNotIn("neko_bilibili_offer", args["reason"])
        self.bot.config.target.allowed_friends = []
        self.bot.ctx.maisaka.proactive.trigger.reset_mock()
        self.assertFalse(await self.bot._do_proactive_chat(self.state, "target"))
        self.bot.ctx.maisaka.proactive.trigger.assert_not_awaited()

    def test_focus_bilibili_material_is_scoped_request_only_and_removed_next_request(self):
        stamp = 1792930000000
        text = visibility.task(stamp).replace(
            "attention_proactive",
            'attention_proactive\n<neko_bilibili_offer receipt_id="' + "a" * 32 + '"/>',
        )
        source = {"session_id": "chat", "items": [visibility.item(text)]}
        found = pending_offers(source, now=stamp / 1000 + 1, source="bilibili")
        self.assertEqual([(r["scope"], r["receipt"]) for r in found], [("chat", "a" * 32)])
        self.assertEqual(
            pending_offers({**source, "session_id": ""}, now=stamp / 1000 + 1, source="bilibili"),
            [],
        )
        clean, _ = filter_request(source)
        self.assertEqual(clean["items"], [])
        injected = inject_bilibili_offers(
            [],
            [
                (
                    "chat",
                    "a" * 32,
                    {
                        "summary": "actual source",
                        "url": "https://www.bilibili.com/video/BV1234567890",
                    },
                )
            ],
        )
        self.assertTrue(injected[0]["meta"]["item_id"].startswith(EPHEMERAL))
        self.assertNotIn("neko.life.share", str(injected))
        self.assertEqual(filter_request({"session_id": "chat", "items": injected})[0]["items"], [])

    async def test_real_sdk_client_signs_for_own_provider_not_life(self):
        from importlib import import_module

        Bridge = import_module(self.mod.__package__ + ".bilibili_bridge").BilibiliBridge
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "bilibili-client.json"
            key.write_text(json.dumps({"neko.proactive": "ab" * 32}), encoding="utf-8")
            key.chmod(0o600)
            calls = []

            async def rpc(method, plugin_id, payload):
                calls.append(payload)
                return {"success": True, "result": {"success": True, "candidate": None}}

            from maibot_sdk.context import PluginPaths

            ctx = PluginContext(
                "neko.proactive",
                rpc_call=rpc,
                paths=PluginPaths(Path(directory), Path(directory) / "runtime"),
            )
            bridge = Bridge(ctx)
            self.assertIsNone(await bridge.claim("chat"))
            args = calls[0]["args"]
            self.assertEqual(args["api_name"], "local.bilibili-autopilot.discovery_v1")
            self.assertEqual(args["args"]["packet"]["audience"], "local.bilibili-autopilot")
            self.assertEqual(bridge.path.name, "bilibili-outbox.json")
