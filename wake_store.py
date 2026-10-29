"""Bounded private wake reservations. No timer, sender or model client."""

import json
import math
import os
from pathlib import Path
import re
import tempfile
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CONTRACT = "neko.tools.context.v1"
MAX_BYTES = 262144
MAX_TARGETS = 128
PENDING = {"scheduled", "blocked"}
STATUSES = PENDING | {"submitted", "rejected", "unknown", "expired", "cancelled", "revoked"}


def text(value, limit, *, empty=False):
    return (
        isinstance(value, str)
        and (empty or bool(value.strip()))
        and len(value) <= limit
        and not any(ord(c) < 32 for c in value)
    )


def number(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1e12


def parse_appointment(at, zone, intent, now):
    if not text(at, 64) or not text(zone, 64) or not text(intent, 240):
        raise ValueError("invalid_time_or_intent")
    moment = datetime.fromisoformat(at)
    if moment.utcoffset() is None:
        raise ValueError("timezone_offset_required")
    try:
        local = moment.astimezone(ZoneInfo(zone))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("invalid_timezone") from exc
    if local.utcoffset() != moment.utcoffset():
        raise ValueError("timezone_offset_mismatch")
    due = moment.timestamp()
    if not 60 <= due - now <= 7 * 86400:
        raise ValueError("time_must_be_60s_to_7d_in_future")
    return due, local.isoformat()


class WakeStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "private-wakes.json"

    def read(self):
        if self.path.is_symlink():
            raise ValueError("unsafe_wake_index")
        if not self.path.exists():
            return {"version": 1, "last_dispatch": 0, "slots": {}}
        if self.path.stat().st_size > MAX_BYTES:
            raise ValueError("wake_index_too_large")
        with self.path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("wake_index_too_large")
        data = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(data, dict)
            or type(data.get("version")) is not int
            or data.get("version") != 1
            or not number(data.get("last_dispatch"))
            or not isinstance(data.get("slots"), dict)
            or len(data["slots"]) > MAX_TARGETS
        ):
            raise ValueError("invalid_wake_index")
        for target, row in data["slots"].items():
            if not text(target, 128) or not isinstance(row, dict):
                raise ValueError("invalid_wake_slot")
            if (
                not text(row.get("session"), 128)
                or not re.fullmatch("[a-f0-9]{32}", str(row.get("id", "")))
                or not isinstance(row.get("status"), str)
                or row.get("status") not in STATUSES
                or not text(row.get("intent"), 240)
                or not text(row.get("timezone"), 64)
                or not text(row.get("at"), 64)
                or not text(row.get("block"), 64, empty=True)
                or not text(row.get("task"), 160, empty=True)
                or type(row.get("shown")) is not bool
                or any(not number(row.get(k)) for k in ("due", "retry", "attempted"))
            ):
                raise ValueError("invalid_wake_record")
        return data

    def write(self, data):
        self.read()  # Corrupt/unknown disk state is never treated as an empty index.
        raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BYTES or len(data["slots"]) > MAX_TARGETS:
            raise ValueError("wake_index_full")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".private-wakes-", dir=self.directory)
        try:
            os.chmod(name, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            if os.name == "posix":
                directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def recover(self, now):
        data = self.read()
        changed = False
        for row in data["slots"].values():
            if row["status"] in PENDING and row["due"] <= now:
                row.update(status="expired", block="restart_missed")
                changed = True
        if changed:
            self.write(data)

    def set(self, data, target, session, at, zone, intent, now):
        due, normalized = parse_appointment(at, zone, intent, now)
        old = data["slots"].get(target, {})
        if target not in data["slots"] and len(data["slots"]) >= MAX_TARGETS:
            raise ValueError("wake_index_full")
        row = {
            "id": uuid.uuid4().hex,
            "session": session,
            "due": due,
            "at": normalized,
            "timezone": zone,
            "intent": intent.strip(),
            "status": "scheduled",
            "retry": due,
            "attempted": old.get("attempted", 0),
            "block": "",
            "task": "",
            "shown": False,
        }
        data["slots"][target] = row
        self.write(data)
        return row
