"""时间与编码口径工具（对应清洗规则 R-02 / S-01、标准化口径 §3.5）。

BTS 的时间字段是 HHMM 字符串（如 "0900"、"2400"），直接当整数相减会在
跨小时处出错（0959 → 1000 差 41），所以必须先换算成"当日分钟数"。
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from typing import Iterable, Iterator
from zoneinfo import ZoneInfo

import pandas as pd

_YM_RE = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


class TimeParseError(ValueError):
    """时间字段非法（E02 脏值）。"""


def _is_blank(value) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if isinstance(value, str) and not value.strip():
        return True
    if value is pd.NaT:
        return True
    return False


def hhmm_to_minutes(value, allow_2400: bool = True) -> tuple[int | None, str]:
    """HHMM → 当日分钟数。

    Returns:
        (minutes | None, status)
        status ∈ {"ok", "empty", "fixed_2400", "invalid"}
        2400 按官方口径视为次日 00:00，本系统统一折算为 1440（跨日 +1440 由调用方合并）。
    """
    if _is_blank(value):
        return None, "empty"
    raw = str(value).strip()
    if raw.endswith(".0"):
        raw = raw[:-2]
    if not raw.isdigit():
        return None, "invalid"
    if len(raw) > 4:
        return None, "invalid"
    raw = raw.zfill(4)
    hour = int(raw[:2])
    minute = int(raw[2:])
    if allow_2400 and hour == 24 and minute == 0:
        return 1440, "fixed_2400"
    if hour >= 24 or minute >= 60:
        return None, "invalid"
    return hour * 60 + minute, "ok"


def minutes_to_hhmm(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    minutes = int(minutes) % 1440
    return f"{minutes // 60:02d}{minutes % 60:02d}"


def parse_ym(value: str) -> str:
    """校验并规范化 yyyy-MM。"""
    if not value or not _YM_RE.match(str(value).strip()):
        raise TimeParseError(f"非法的月份：{value!r}，应为 yyyy-MM")
    ym = str(value).strip()
    if not (1 <= int(ym[5:]) <= 12):
        raise TimeParseError(f"非法的月份：{ym}")
    return ym


def parse_date(value: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not _DATE_RE.match(text):
        raise TimeParseError(f"非法的日期：{value!r}，应为 yyyy-MM-dd")
    return datetime.strptime(text, "%Y-%m-%d").date()


def ym_of(value) -> str:
    """任意日期 / 日期字符串 / Timestamp → yyyy-MM。"""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m")
    text = str(value).strip()
    if _YM_RE.match(text):
        return text
    return parse_date(text).strftime("%Y-%m")


def iter_months(start_ym: str, end_ym: str) -> list[str]:
    """生成 [start, end] 闭区间的月份列表。"""
    start = datetime.strptime(parse_ym(start_ym), "%Y-%m")
    end = datetime.strptime(parse_ym(end_ym), "%Y-%m")
    if start > end:
        return []
    out: list[str] = []
    cur = start
    while cur <= end:
        out.append(cur.strftime("%Y-%m"))
        cur = cur.replace(year=cur.year + 1, month=1) if cur.month == 12 else cur.replace(month=cur.month + 1)
    return out


def prev_month(ym: str) -> str:
    dt = datetime.strptime(parse_ym(ym), "%Y-%m")
    prev = dt.replace(year=dt.year - 1, month=12) if dt.month == 1 else dt.replace(month=dt.month - 1)
    return prev.strftime("%Y-%m")


def slot_index(ts: datetime, slot_minutes: int = 30) -> int:
    """当日时段序号（0 起）。"""
    return (ts.hour * 60 + ts.minute) // slot_minutes


def slot_label(day: date, slot_minutes: int = 30, slot: int = 0) -> str:
    return f"{day.strftime('%Y-%m-%d')}#{slot:02d}"


def utc_to_local(ts, tz_name: str | None):
    """UTC 时间戳 → 机场本地时区（展示用，§3.5 时间口径）。"""
    if _is_blank(ts):
        return None
    if isinstance(ts, str):
        ts = pd.Timestamp(ts).to_pydatetime()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    try:
        tz = ZoneInfo(tz_name or "UTC")
    except Exception:  # 时区名非法时退回 UTC，不让展示层崩
        tz = ZoneInfo("UTC")
    return ts.astimezone(tz)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def local_departure_utc(value, tz_name: str | None):
    """BTS 本地计划时间转成 UTC 无时区时间，与 NOAA obs_hour 一致。

    缺失时区、夏令时重复/不存在的本地时间不猜测，返回 NaT。
    """
    if not isinstance(tz_name, str) or not tz_name or pd.isna(value):
        return pd.NaT
    try:
        return (pd.Timestamp(value).tz_localize(tz_name, ambiguous="NaT", nonexistent="NaT")
                .tz_convert("UTC").tz_localize(None))
    except (ValueError, KeyError):
        return pd.NaT


def now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def duration_text(seconds: float | None) -> str:
    if not seconds:
        return "0s"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60}m"


def chunked(items: Iterable, size: int) -> Iterator[list]:
    buf: list = []
    for it in items:
        buf.append(it)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
