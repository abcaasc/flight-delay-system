"""指标体系口径（唯一真源）。

设计说明书 §5 与开题报告都要求"指标口径一致"，因此 ETL、接口、AI 摘要三处共用
本文件，任何口径调整只改这里。

标准定义：
    scheduled  计划航班量 = 全部记录数
    cancelled  取消量
    operated   实际执行 = scheduled - cancelled
    delayed    延误量 = operated 中 DepDelay ≥ 阈值（默认 15 分钟）
    ontime_rate = (operated - delayed) / operated    ← 准点率（分母不含取消）
    cancel_rate = cancelled / scheduled
    delay_rate  = delayed / operated
    avg_delay   = operated 的 DepDelay 均值（保留负值，提前起飞计负）
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from app.config import Config


def safe_div(num, den):
    """除零安全：分母为 0 时返回 NaN（页面显示为 '—' 而不是 0%）。"""
    try:
        return num / den if den else float("nan")
    except Exception:
        return float("nan")


def rate(num, den, digits: int = 4) -> float | None:
    value = safe_div(num, den)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return round(float(value), digits)


def compute_metrics(df: pd.DataFrame, ontime_threshold: int | None = None) -> dict:
    """从 DWD 明细算出一组标准指标。"""
    threshold = ontime_threshold or Config.ONTIME_THRESHOLD_MIN
    if df is None or len(df) == 0:
        return {
            "scheduled": 0, "operated": 0, "cancelled": 0, "delayed": 0, "severe": 0,
            "ontime_rate": None, "cancel_rate": None, "delay_rate": None,
            "avg_delay_min": None, "median_delay_min": None, "p90_delay_min": None,
            "avg_arr_delay_min": None, "flights_with_chain": 0, "weather_coverage": None,
        }

    scheduled = int(len(df))
    cancelled = int((df.get("cancelled", pd.Series(0, index=df.index)) == 1).sum())
    operated_mask = df.get("cancelled", pd.Series(0, index=df.index)) != 1
    operated_df = df[operated_mask]
    operated = int(len(operated_df))

    delay_col = operated_df.get("dep_delay_min")
    if delay_col is not None:
        delayed = int((delay_col >= threshold).sum())
        severe = int((delay_col > Config.SEVERE_DELAY_MIN).sum())
        avg_delay = float(delay_col.mean()) if delay_col.notna().any() else None
        median_delay = float(delay_col.median()) if delay_col.notna().any() else None
        p90 = float(delay_col.quantile(0.90)) if delay_col.notna().any() else None
    else:
        delayed = severe = 0
        avg_delay = median_delay = p90 = None

    arr_col = operated_df.get("arr_delay_min")
    avg_arr = float(arr_col.mean()) if arr_col is not None and arr_col.notna().any() else None

    chain = int((df.get("chain_available", pd.Series(0, index=df.index)) == 1).sum())
    weather = None
    if "weather_available" in df.columns and len(df):
        weather = round(float(df["weather_available"].fillna(0).mean()), 4)

    return {
        "scheduled": scheduled,
        "operated": operated,
        "cancelled": cancelled,
        "delayed": delayed,
        "severe": severe,
        "ontime_rate": rate(operated - delayed, operated),
        "cancel_rate": rate(cancelled, scheduled),
        "delay_rate": rate(delayed, operated),
        "avg_delay_min": round(avg_delay, 2) if avg_delay is not None else None,
        "median_delay_min": round(median_delay, 2) if median_delay is not None else None,
        "p90_delay_min": round(p90, 2) if p90 is not None else None,
        "avg_arr_delay_min": round(avg_arr, 2) if avg_arr is not None else None,
        "flights_with_chain": chain,
        "weather_coverage": weather,
    }


def delta_pct(current: float | None, previous: float | None) -> float | None:
    """环比变化率（%），用于 P3 排行页的 delta 列与看板"较上周是否恶化"。"""
    if current is None or previous is None:
        return None
    if previous == 0:
        return None
    return round((current - previous) / abs(previous) * 100, 2)


def risk_score(delay_rate: float | None, avg_delay: float | None, weather_class: int | None) -> float:
    """机场×时段的综合风险分（0–100），供网络图着色与高风险高亮。

    权重：延误率 0.6 + 平均延误（相对 60 分钟归一） 0.3 + 天气等级 0.1。
    权重可外置；此处给的是可解释的线性合成，答辩时能讲清每一项的物理含义。
    """
    dr = 0.0 if delay_rate is None or (isinstance(delay_rate, float) and math.isnan(delay_rate)) else float(delay_rate)
    ad = 0.0 if avg_delay is None or (isinstance(avg_delay, float) and math.isnan(avg_delay)) else float(avg_delay)
    wc = 0 if weather_class in (None, -1) or (isinstance(weather_class, float) and math.isnan(weather_class)) else int(weather_class)
    weather_penalty = min(wc, 6) / 6.0
    score = 0.6 * min(dr, 1.0) + 0.3 * min(max(ad, 0) / 60.0, 1.0) + 0.1 * weather_penalty
    return round(score * 100, 2)


def delay_level_label(level: int | None) -> str:
    return {-1: "取消", 0: "准点(<15min)", 1: "轻微延误(15-60min)", 2: "严重延误(>60min)"}.get(
        int(level) if level is not None and not (isinstance(level, float) and math.isnan(level)) else -2,
        "未知",
    )


def bucket_delay(values: pd.Series) -> list[dict]:
    """延误分布直方图（页面 P1 的分布图）。"""
    bins = [-math.inf, -15, 0, 15, 30, 60, 120, 240, 480, math.inf]
    labels = ["提前>15min", "提前0-15min", "延误0-15min", "延误15-30min", "延误30-60min",
              "延误1-2h", "延误2-4h", "延误4-8h", "延误>8h"]
    if values is None or values.empty:
        return [{"label": l, "count": 0} for l in labels]
    cut = pd.cut(values.dropna(), bins=bins, labels=labels, right=False)
    counts = cut.value_counts().reindex(labels, fill_value=0)
    total = int(counts.sum()) or 1
    return [{"label": str(label), "count": int(cnt),
             "pct": round(int(cnt) / total * 100, 2)} for label, cnt in counts.items()]
