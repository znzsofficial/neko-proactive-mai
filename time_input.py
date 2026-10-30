"""Small, deterministic Beijing-time input adapter; no natural-language guessing."""

from datetime import datetime, timedelta
import re
from zoneinfo import ZoneInfo

from .wake_store import text

DEFAULT_ZONE = "Asia/Shanghai"


def simple_instant(value, now, *, today_only=False):
    if not text(value, 64):
        raise ValueError("simple_today_time" if today_only else "simple_wake_time")
    value = value.strip()
    current = datetime.fromtimestamp(now, ZoneInfo(DEFAULT_ZONE))
    try:
        clock = re.fullmatch(r"(?:(今天|明天)\s*)?([01]?\d|2[0-3]):([0-5]\d)", value)
        relative = re.fullmatch(r"([1-9]\d{0,4})\s*(分钟|小时)后", value)
        if clock:
            day = current.date() + timedelta(days=clock[1] == "明天")
            moment = datetime.combine(day, datetime.min.time(), tzinfo=current.tzinfo).replace(
                hour=int(clock[2]), minute=int(clock[3])
            )
        elif relative and not today_only:
            seconds = int(relative[1]) * (60 if relative[2] == "分钟" else 3600)
            moment = current + timedelta(seconds=seconds)
        elif not today_only:
            # Explicit dated times and old offset ISO inputs remain unambiguous.
            if not re.fullmatch(
                r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(?::\d\d(?:\.\d{1,6})?)?(?:Z|[+-]\d\d:\d\d)?", value
            ):
                raise ValueError("simple_wake_time")
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=current.tzinfo)
            moment = moment.astimezone(current.tzinfo)
        else:
            raise ValueError("simple_today_time")
    except (ValueError, OverflowError):
        raise ValueError("simple_today_time" if today_only else "simple_wake_time") from None
    if today_only and moment.date() != current.date():
        raise ValueError("simple_today_time")
    if not 60 <= moment.timestamp() - now <= 7 * 86400:
        raise ValueError("simple_today_time" if today_only else "simple_wake_time")
    return moment.isoformat()


def convert_zone(at, zone):
    return datetime.fromisoformat(at).astimezone(ZoneInfo(zone)).isoformat()
