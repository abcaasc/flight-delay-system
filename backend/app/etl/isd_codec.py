"""NOAA ISD 编码解析（气象要素）与 7 类天气编码。

ISD 的气象要素是"逗号分隔的定长编码 + 缺测标记"，例如：
    WND = "070,1,N,0036,1"   风向 70°，风速 3.6 m/s
    CIG = "22000,5,9,N"      云底高 22000 m
    VIS = "016000,1,9,9"     能见度 16000 m
    TMP = "+0150,1"          气温 15.0 ℃（×0.1）
    SLP = "10172,1"          海平面气压 1017.2 hPa
缺测统一表现为 99999 / +9999 / 9。

7 类天气编码与 STPN 中国域对齐（S-09 跨域特征同构）：
    0 normal / 1 rain / 2 cloud / 3 thunderstorm / 4 fog / 5 storm / 6 snow
"""
from __future__ import annotations

import math
import re

MISSING_VALUES = {"99999", "9999", "+9999", "-9999", "999999", ""}

WEATHER_CLASS = {
    0: "normal", 1: "rain", 2: "cloud", 3: "thunderstorm",
    4: "fog", 5: "storm", 6: "snow",
}

# 阈值（可被调用方覆盖）
VIS_FOG_M = 1600.0          # 能见度 < 1600 m ≈ 1 mile 视为雾
WIND_STORM_MS = 10.8        # ≥ 10.8 m/s（24 mph）视为大风
CEIL_CLOUDY_M = 914.0       # 云底高 < 3000 ft 视为低云


def _parts(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [p.strip() for p in str(raw).split(",")]


def _num(text: str | None) -> float | None:
    if text is None:
        return None
    text = str(text).strip()
    if text in MISSING_VALUES:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_wnd(raw: str | None) -> tuple[float | None, float | None, float | None]:
    """WND → (风向°, 风速 m/s, 阵风 m/s)。"""
    parts = _parts(raw)
    if len(parts) < 4:
        return None, None, None
    direction = _num(parts[0])
    speed = _num(parts[3])
    gust = _num(parts[5]) if len(parts) > 5 else None
    if direction is not None and direction == 999:
        direction = None
    if speed is not None:
        speed = round(speed / 10.0, 2)  # 编码单位是 0.1 m/s
    if gust is not None:
        gust = round(gust / 10.0, 2)
    return direction, speed, gust


def parse_cig(raw: str | None) -> float | None:
    """CIG → 云底高（米）。99999 = 无云/缺测。"""
    parts = _parts(raw)
    if not parts:
        return None
    value = _num(parts[0])
    if value is None or value >= 99999:
        return None
    return value


def parse_vis(raw: str | None) -> float | None:
    """VIS → 能见度（米）。"""
    parts = _parts(raw)
    if not parts:
        return None
    value = _num(parts[0])
    if value is None or value >= 999999:
        return None
    return value


def parse_tmp(raw: str | None) -> float | None:
    """TMP / DEW → 摄氏温度（编码 ×0.1）。"""
    parts = _parts(raw)
    if not parts:
        return None
    value = _num(parts[0].replace("+", ""))
    if value is None or abs(value) >= 9999:
        return None
    return round(value / 10.0, 1)


def parse_slp(raw: str | None) -> float | None:
    """SLP → 海平面气压 hPa（编码 ×0.1）。"""
    parts = _parts(raw)
    if not parts:
        return None
    value = _num(parts[0])
    if value is None or value >= 99999:
        return None
    return round(value / 10.0, 1)


def parse_aa1(raw: str | None) -> float | None:
    """AA1 → 液态降水深度（毫米）。

    AA1 的元素顺序是 (时段, 时段QC, 深度, 深度QC, 状态, 状态QC)；深度单位 0.1 mm。
    实测两种形态：
        "01,0000,9,5"          → 深度在 parts[1]
        "99,9,+01250,1,99,9"   → parts[1] 是 1 位 QC，深度在 parts[2]
    用"第二位是否单字符"判定，覆盖两种真实写法。
    """
    parts = _parts(raw)
    if len(parts) < 3:
        return None
    depth_text = parts[1]
    if len(depth_text) <= 1 and len(parts) > 2:
        depth_text = parts[2]
    value = _num(depth_text.replace("+", ""))
    if value is None:
        return None
    return round(value / 10.0, 1)


def parse_sky(remarks: str | None) -> str:
    """从 REM 备注/METAR 里提取天空状况关键词（用于云量判断）。"""
    if not remarks:
        return ""
    match = re.search(r"\b(?:BKN|OVC|SCT|FEW|VV)\d{3}\b", remarks)
    return match.group(1) if match else ""


def classify_weather(vis_m: float | None, ceil_m: float | None, wind_ms: float | None,
                     temp_c: float | None, precip_mm: float | None,
                     remarks: str | None = None) -> int:
    """7 类天气编码（0 normal / 1 rain / 2 cloud / 3 thunderstorm / 4 fog / 5 storm / 6 snow）。"""
    text = (remarks or "").upper()
    has_ts = "TS" in text
    has_snow = bool(re.search(r"\b(?:SN|SG|PL|GS)\b", text)) or (
        temp_c is not None and temp_c <= 0.0 and (precip_mm or 0) > 0
    )
    if has_ts:
        return 3
    if has_snow:
        return 6
    if vis_m is not None and vis_m < VIS_FOG_M:
        return 4
    if wind_ms is not None and wind_ms >= WIND_STORM_MS:
        return 5
    if precip_mm is not None and precip_mm > 0:
        return 1
    if (ceil_m is not None and ceil_m < CEIL_CLOUDY_M) or parse_sky(remarks) in {"OVC", "BKN", "VV"}:
        return 2
    return 0


def classify_vectorized(vis_m, ceil_m, wind_ms, temp_c, precip_mm, remarks):
    """classify_weather 的向量化版本（ETL 里对百万行调用，避免逐行 apply）。

    参数都是 pandas Series（或可广播标量），返回 Series[int]。
    口径与 classify_weather 完全一致，单测里做逐行比对。
    """
    import numpy as np
    import pandas as pd

    text = remarks.fillna("").astype(str).str.upper() if hasattr(remarks, "fillna") else pd.Series([""])
    has_ts = text.str.contains(r"\bTS", regex=True)
    has_snow = text.str.contains(r"\b(?:SN|SG|PL|GS)\b", regex=True) | (
        (temp_c <= 0.0).fillna(False).astype(bool) & (precip_mm.fillna(0) > 0)
    )
    fog = (vis_m < VIS_FOG_M).fillna(False).astype(bool)
    storm = (wind_ms >= WIND_STORM_MS).fillna(False).astype(bool)
    rainy = (precip_mm.fillna(0) > 0)
    ceil_missing = ceil_m.isna() if hasattr(ceil_m, "isna") else None
    cloudy = ((ceil_m < CEIL_CLOUDY_M) if ceil_missing is None else
              ((ceil_m < CEIL_CLOUDY_M) & (~ceil_missing))).fillna(False).astype(bool) | \
             text.str.contains(r"\b(?:OVC|BKN|VV)\d", regex=True)

    out = np.zeros(len(text), dtype="int64")
    out[cloudy.to_numpy()] = 2
    out[rainy.to_numpy()] = 1
    out[storm.to_numpy()] = 5
    out[fog.to_numpy()] = 4
    out[has_snow.to_numpy()] = 6
    out[has_ts.to_numpy()] = 3
    return pd.Series(out, index=text.index if hasattr(text, "index") else None)


def meters_to_miles(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value / 1609.344, 3)


def meters_to_feet(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 3.28084, 1)


def is_missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value))

