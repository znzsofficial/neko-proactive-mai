"""Opt-in Life candidate bridge; persistent attribution, never another timer."""

import asyncio
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

CLIENT_ID = "neko.proactive"
LEGACY_CLIENT_ID = "attention-seeker-mai"


class LifeBridge:
    def __init__(self, context):
        self.context = context
        self.directory = Path(context.paths.data_dir)
        self.path = self.directory / "life-outbox.json"
        self.lock = asyncio.Lock()

    def _keys(self):
        p = self.directory / "life-client.json"
        if (
            p.is_symlink()
            or p.stat().st_size > 1024
            or (os.name == "posix" and p.stat().st_mode & 0o077)
        ):
            raise ValueError("unsafe_key_file")
        keys = json.loads(p.read_text(encoding="utf-8"))
        key = keys.get(CLIENT_ID) or keys.get(LEGACY_CLIENT_ID)
        if not isinstance(key, str) or not re.fullmatch("[a-f0-9]{64}", key):
            raise ValueError("invalid_key")
        return key

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.is_symlink() or self.path.stat().st_size > 128000:
            raise ValueError("unsafe_outbox")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or len(value) > 128:
            raise ValueError("invalid_outbox")
        return value

    def _write(self, rows):
        self._read()  # Never overwrite corrupt state as empty.
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".life-outbox-", dir=self.directory)
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(rows, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    async def call(self, operation, body):
        key = await asyncio.to_thread(self._keys)
        packet = {
            "v": 1,
            "client": CLIENT_ID,
            "audience": "neko.life",
            "issued": int(time.time()),
            "nonce": uuid.uuid4().hex,
            "operation": operation,
            "body": body,
        }
        raw = json.dumps(
            packet,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        packet["signature"] = hmac.new(
            bytes.fromhex(key), raw, hashlib.sha256
        ).hexdigest()
        async with asyncio.timeout(2):
            response = await self.context.call_capability(
                "api.call",
                api_name="neko.life.sharing_v1",
                version="1",
                args={"packet": packet},
            )
        value = response  # SDK api.call already unwraps the Host envelope.
        if not isinstance(value, dict) or value.get("success") is not True:
            raise ValueError("life_unavailable")
        return value

    async def claim(self, session):
        async with self.lock:
            rows = await asyncio.to_thread(self._read)
            # Keep all uncertain rows. Refuse new claims rather than evict attribution.
            rows = {
                k: v
                for k, v in rows.items()
                if v.get("status") not in {"sent", "released"}
                or time.time() - v.get("created", 0) < 7 * 86400
            }
            if len(rows) >= 128:
                return None
            result = await self.call("claim", {"session_id": session})
            candidate = result.get("candidate")
            if candidate is None:
                return None
            if (
                not isinstance(candidate, dict)
                or not isinstance(candidate.get("receipt_id"), str)
                or not re.fullmatch("[a-f0-9]{32}", candidate["receipt_id"])
                or not isinstance(candidate.get("summary"), str)
                or len(candidate["summary"]) > 500
            ):
                raise ValueError("invalid_candidate")
            key = candidate["receipt_id"]
            rows[key] = {
                "session_id": session,
                "receipt_id": key,
                "status": "prepared",
                "host_task": "",
                "created": time.time(),
                "acknowledged": False,
            }
            await asyncio.to_thread(self._write, rows)
            return candidate

    async def update(self, receipt, status, task=""):
        async with self.lock:
            rows = await asyncio.to_thread(self._read)
            if receipt not in rows:
                return
            row = rows[receipt]
            if row["status"] == "sent":
                return
            row.update(
                status=status, host_task=task or row["host_task"], acknowledged=False
            )
            await asyncio.to_thread(self._write, rows)
            if status in {"submitted", "unknown", "released", "sent"}:
                await self._flush_row(row)
                rows[receipt]["acknowledged"] = True
                await asyncio.to_thread(self._write, rows)

    async def _flush_row(self, row):
        # Ensure a sent receipt can be recognized even after a lost submit ack.
        if row["status"] == "sent":
            await self.call(
                "receipt",
                {k: row[k] for k in ("session_id", "receipt_id", "host_task")}
                | {"status": "submitted"},
            )
        await self.call(
            "receipt",
            {k: row[k] for k in ("session_id", "receipt_id", "status", "host_task")},
        )

    async def flush(self):
        async with self.lock:
            rows = await asyncio.to_thread(self._read)
            for row in [r for r in rows.values() if not r.get("acknowledged")][:2]:
                if row["status"] == "prepared":
                    # Crash before/after trigger cannot be distinguished safely.
                    row.update(status="unknown", acknowledged=False)
                if not row.get("acknowledged"):
                    try:
                        await self._flush_row(row)
                        row["acknowledged"] = True
                    except Exception:
                        break
            await asyncio.to_thread(self._write, rows)

    async def after_send(self, session, task, sent):
        if (
            sent is not True
            or not isinstance(task, str)
            or not task.startswith(
                ("proactive:neko.proactive:", "proactive:attention-seeker-mai:")
            )
        ):
            return
        async with self.lock:
            rows = await asyncio.to_thread(self._read)
            matching = [
                r
                for r in rows.values()
                if r["session_id"] == session and r["host_task"] == task
            ]
        if len(matching) == 1:
            await self.update(matching[0]["receipt_id"], "sent", task)
