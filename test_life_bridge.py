import hashlib
import hmac
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock

from life_bridge import LifeBridge


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.key = "56" * 32
        p = Path(self.temp.name) / "life-client.json"
        p.write_text(json.dumps({"neko.proactive": self.key}), encoding="utf-8")
        p.chmod(0o600)
        self.context = types.SimpleNamespace(
            paths=types.SimpleNamespace(data_dir=self.temp.name),
            call_capability=AsyncMock(
                return_value={
                    "success": True,
                    "candidate": None,
                }
            ),
        )
        self.bridge = LifeBridge(self.context)

    async def test_signed_operation_binds_audience_client_and_body(self):
        await self.bridge.call("claim", {"session_id": "chat"})
        packet = self.context.call_capability.call_args.kwargs["args"]["packet"]
        signature = packet.pop("signature")
        raw = json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        self.assertEqual(
            signature,
            hmac.new(bytes.fromhex(self.key), raw, hashlib.sha256).hexdigest(),
        )
        self.assertEqual(packet["audience"], "neko.life")
        self.assertEqual(packet["client"], "neko.proactive")

    async def claim(self):
        candidate = {
            "receipt_id": "a" * 32,
            "summary": "合成公开见闻",
            "topic": "公开主题",
            "sources": [],
        }
        self.context.call_capability.return_value = {
            "success": True,
            "candidate": candidate,
        }
        return await self.bridge.claim("chat")

    async def test_claim_durable_before_host_trigger_and_crash_becomes_unknown(self):
        await self.claim()
        saved = json.loads(self.bridge.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["a" * 32]["status"], "prepared")
        reopened = LifeBridge(self.context)
        await reopened.flush()
        saved = json.loads(self.bridge.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["a" * 32]["status"], "unknown")

    async def test_only_exact_task_and_session_ack_can_mark_sent(self):
        await self.claim()
        task = "proactive:neko.proactive:1700000000000"
        await self.bridge.update("a" * 32, "submitted", task)
        for session, anchor, sent in [
            ("other", task, True),
            ("chat", "real-qq-id", True),
            ("chat", task, False),
            ("chat", task, "true"),
        ]:
            await self.bridge.after_send(session, anchor, sent)
        self.assertEqual(self.bridge._read()["a" * 32]["status"], "submitted")
        await self.bridge.after_send("chat", task, True)
        self.assertEqual(self.bridge._read()["a" * 32]["status"], "sent")

    async def test_failed_receipt_keeps_outbox_not_false_success(self):
        await self.claim()
        self.context.call_capability.side_effect = TimeoutError
        with self.assertRaises(TimeoutError):
            await self.bridge.update(
                "a" * 32, "submitted", "proactive:neko.proactive:1700000000000"
            )
        row = self.bridge._read()["a" * 32]
        self.assertEqual(row["status"], "submitted")
        self.assertFalse(row["acknowledged"])

    async def test_corrupt_outbox_preserved_not_overwritten(self):
        self.bridge.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            await self.bridge.claim("chat")
        self.assertEqual(self.bridge.path.read_text(encoding="utf-8"), "{broken")

    async def test_key_file_missing_means_no_api_call(self):
        (Path(self.temp.name) / "life-client.json").unlink()
        with self.assertRaises(OSError):
            await self.bridge.call("claim", {"session_id": "chat"})
        self.context.call_capability.assert_not_awaited()
