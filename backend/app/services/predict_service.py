"""单航班预测的数据装配：把接口输入 + 历史上下文 + 气象 → 一行特征。

要点（对应 S-06 时序防泄漏）：
    * 历史延误率只取"预测时刻之前"的滞后 7 日统计（来自特征宽表）；
    * 气象优先用当日真实观测窗口；未来/无观测时回落到同机场同小时的月度气候态，
      并置 weather_available=0，让模型（和用户）知道这条特征不可靠。
"""
from __future__ import annotations

import math
from datetime import date

import numpy as np
import pandas as pd

from app.config import Config
from app.services import engine, model as model_service, stats
from app.utils.logger import get_logger
from app.utils.timeutil import hhmm_to_minutes, parse_date, local_departure_utc

log = get_logger(__name__)


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


def route_distance(origin: str, dest: str) -> float | None:
    dim = stats.airport_dim()
    a, b = dim.get(origin, {}), dim.get(dest, {})
    if None in (a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")):
        return None
    return haversine_miles(a["lat"], a["lon"], b["lat"], b["lon"])


def route_reference_distance(origin: str, dest: str, period: str | None = None) -> float | None:
    """优先用历史航线距离（DWS），没有再按经纬度算大圆距离。"""
    period = period or engine.BaseEngine.latest_period("dws_route_daily")
    if period:
        try:
            df = engine.read("dws_route_daily",
                             columns=["route_key", "distance_mi"],
                             filters=[("year_month", "=", period)])
            hit = df[df["route_key"].astype(str) == f"{origin}-{dest}"]
            if not hit.empty:
                distances = pd.to_numeric(hit["distance_mi"], errors="coerce")
                distances = distances[np.isfinite(distances) & (distances > 0)]
                if not distances.empty:
                    return float(distances.median())
        except Exception:
            pass
    return route_distance(origin, dest)


def weather_climatology(airport: str, hour: int, period: str | None = None) -> dict:
    """同机场同小时的气候态（无当日观测时的回落方案）。"""
    period = period or engine.BaseEngine.latest_period("dws_airport_hourly")
    empty = {"vis_min_mi": 9.0, "wind_max_ms": 5.0, "tmp_mean_c": 15.0, "precip_mm": 0.0,
             "weather_class": 0, "weather_available": 0}
    if not period:
        return empty
    try:
        df = engine.read("dws_airport_hourly", filters=[("year_month", "=", period)])
    except Exception:
        return empty
    if df.empty:
        return empty
    sub = df[df["airport_iata"].astype(str) == airport]
    if sub.empty:
        return empty
    by_hour = sub[pd.to_numeric(sub["stat_hour"], errors="coerce") == hour]
    use = by_hour if not by_hour.empty else sub
    out = dict(empty)
    for key, col in (("vis_min_mi", "vis_mean_mi"), ("wind_max_ms", "wind_max_ms"),
                     ("tmp_mean_c", "tmp_mean_c"), ("precip_mm", "precip_sum_mm")):
        values = pd.to_numeric(use[col], errors="coerce")
        values = values[np.isfinite(values)]
        if not values.empty:
            out[key] = float(values.median())
    out["weather_available"] = 0
    return out


def actual_weather_window(airport: str, day: date, dep_minutes: int) -> dict | None:
    """有历史观测时，取该机场当天起飞前 1–3 小时的真实窗口。"""
    anchor = local_departure_utc(pd.Timestamp(day) + pd.Timedelta(minutes=dep_minutes),
                                 stats.airport_dim().get(airport, {}).get("tz"))
    if pd.isna(anchor):
        return None
    anchor = anchor.floor("h")
    start = anchor - pd.Timedelta(hours=3)
    try:
        df = engine.read("dwd_weather_hourly",
                         filters=[("year", "in", sorted({str(anchor.year), str(start.year)})),
                                  ("airport_iata", "=", airport),
                                  ("obs_hour", ">=", start.strftime("%Y-%m-%d %H:%M")),
                                  ("obs_hour", "<=", (anchor - pd.Timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"))])
    except Exception:
        return None
    if df.empty:
        return None
    df["obs_hour"] = pd.to_datetime(df["obs_hour"], errors="coerce")
    window = df[(df["obs_hour"] >= anchor - pd.Timedelta(hours=3))
                & (df["obs_hour"] <= anchor - pd.Timedelta(hours=1))]
    if window.empty:
        return None
    if not any(np.isfinite(pd.to_numeric(window[c], errors="coerce")).any()
               for c in ("vis_mi", "wind_ms", "temp_c", "precip_mm")):
        return None

    def aggregate(column: str, operation: str, default: float) -> float:
        values = pd.to_numeric(window[column], errors="coerce")
        values = values.where(np.isfinite(values)).dropna()
        return default if values.empty else float(getattr(values, operation)())

    return {
        "vis_min_mi": aggregate("vis_mi", "min", 9.0),
        "wind_max_ms": aggregate("wind_ms", "max", 0.0),
        "tmp_mean_c": aggregate("temp_c", "mean", 15.0),
        "precip_mm": aggregate("precip_mm", "sum", 0.0),
        "weather_class": int(aggregate("weather_class", "max", 0)),
        "weather_available": 1,
    }



def build_feature_row(origin: str, dest: str, airline: str, flight_date: str,
                      crs_dep_time: str, distance_mi: float | None = None,
                      with_weather: bool = True) -> tuple[pd.DataFrame, dict]:
    """返回 (单行特征 DataFrame, 特征装配说明)。"""
    origin, dest, airline = origin.upper(), dest.upper(), airline.upper()
    day = parse_date(flight_date)
    dep_minutes, status = hhmm_to_minutes(crs_dep_time)
    if dep_minutes is None:
        raise ValueError(f"计划起飞时间非法：{crs_dep_time!r}（应为 HHMM，如 0830）")
    dep_minutes = int(dep_minutes)
    hour = min(dep_minutes // 60, 23)
    slot = min(dep_minutes // Config.CHINA_SLOT_MINUTES, 1440 // Config.CHINA_SLOT_MINUTES - 1)

    if distance_mi is not None and (not math.isfinite(distance_mi) or distance_mi <= 0):
        raise ValueError("distance_mi 必须是大于 0 的有限数值")
    context = model_service.feature_context(as_of=day.isoformat())
    route_key = f"{origin}-{dest}"

    def _pick(mapping: dict, *keys, default=0.5):
        for key in keys:
            if key in mapping and mapping[key] is not None and math.isfinite(float(mapping[key])):
                return float(mapping[key])
        return default

    origin_rate = _pick(context["origin"], origin)
    dest_rate = _pick(context["dest"], dest, default=origin_rate)
    route_rate = _pick(context["route"], route_key, default=(origin_rate + dest_rate) / 2)
    airline_rate = _pick(context["airline"], airline, default=origin_rate)
    traffic = context["traffic"].get(origin)

    reference_distance = route_reference_distance(origin, dest) if distance_mi is None else None
    dist = distance_mi if distance_mi is not None else reference_distance or 800.0
    # 巡航 800 km/h ≈ 497 mph，加 30 分钟滑行作为计划飞行时长估算
    elapsed = round(dist / 497 * 60 + 30, 1)

    holiday = 0
    try:
        dim_date = engine.read("dim_date", columns=["date_key", "is_holiday"],
                               filters=[("year", "=", str(day.year))])
        hit = dim_date[dim_date["date_key"].astype(str) == day.strftime("%Y-%m-%d")]
        if not hit.empty:
            holiday = int(bool(hit["is_holiday"].iloc[0]))
    except Exception:
        pass

    weather = actual_weather_window(origin, day, dep_minutes) if with_weather else None
    weather_source = "observation"
    if weather is None:
        # 只用预测日期之前已完整结束的月份，避免历史回放读取未来气象。
        previous_periods = [p for p in engine.BaseEngine.available_periods("dws_airport_hourly")
                            if p < day.strftime("%Y-%m")]
        weather = weather_climatology(origin, hour, previous_periods[-1]) if previous_periods else {
            "vis_min_mi": 9.0, "wind_max_ms": 5.0, "tmp_mean_c": 15.0, "precip_mm": 0.0,
            "weather_class": 0, "weather_available": 0}
        weather_source = "climatology" if previous_periods else "default"

    row = {
        "origin": origin, "dest": dest, "route_key": route_key,
        "reporting_airline": airline, "flight_date": day.strftime("%Y-%m-%d"),
        "dep_hour": hour, "dep_slot": slot, "month": day.month,
        "day_of_week": day.weekday(), "is_weekend": int(day.weekday() >= 5),
        "is_holiday": holiday,
        "dow_sin": math.sin(2 * math.pi * day.weekday() / 7),
        "dow_cos": math.cos(2 * math.pi * day.weekday() / 7),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "distance_mi": float(dist), "crs_elapsed_min": elapsed,
        "origin_traffic_24h": float(traffic) if traffic is not None else np.nan,
        "origin_delay_rate_prev_day": _pick(context.get("origin_prev", {}), origin),
        "origin_delay_rate_7d": origin_rate,
        "dest_delay_rate_7d": dest_rate,
        "route_delay_rate_7d": route_rate,
        "airline_delay_rate_7d": airline_rate,
        # 单航班在线预测拿不到"本班前序航班"（需实时航班链），按不可用处理
        "chain_available": 0, "prev_arr_delay_min": np.nan,
        "turnaround_min": np.nan, "buffer_ratio": np.nan, "risk_flag": 0,
        "weather_available": weather["weather_available"],
        "vis_min_mi": weather["vis_min_mi"], "wind_max_ms": weather["wind_max_ms"],
        "tmp_mean_c": weather["tmp_mean_c"], "precip_mm": weather["precip_mm"],
        "weather_class": weather["weather_class"],
    }
    features = pd.DataFrame([row])
    return features, {
        "time": {"dep_minutes": dep_minutes, "hhmm_status": status, "hour": hour, "slot": slot},
        "rate_source_period": context.get("period"),
        "weather_source": weather_source,
        "distance_source": "input" if distance_mi is not None else "reference" if reference_distance else "default",
        "note": "在线单航班预测不含航班链特征（chain_available=0）；与离线预测存在特征可用性差异。"
                "pred_minutes 为概率映射参考值，pred_level 为风险分档，并非独立回归或三级分类模型输出。",
    }


def predict_single(origin: str, dest: str, airline: str, flight_date: str,
                   crs_dep_time: str, distance_mi: float | None = None,
                   top_k: int = 10) -> dict:
    features, meta = build_feature_row(origin, dest, airline, flight_date, crs_dep_time,
                                       distance_mi)
    result = model_service.predict_frame(features)
    prob = float(result["prob"][0])
    contrib = result["contrib"].iloc[0]
    factors = model_service.top_factors(contrib, prob, k=top_k)
    threshold = result["threshold"]
    bias = float(result["base_logit"][0])
    baseline_prob = float(1 / (1 + math.exp(-bias)))

    return {
        "flight": {"origin": origin.upper(), "dest": dest.upper(),
                   "airline": airline.upper(), "flight_date": flight_date,
                   "crs_dep_time": crs_dep_time},
        "prediction": {
            "prob_delay": round(prob, 4),
            "pred_class": int(prob >= threshold),
            "pred_level": int(model_service._level3_from_threshold(np.array([prob]), threshold)[0]),
            "pred_minutes": float(model_service._pred_minutes(np.array([prob]))[0]),
            "risk_level": model_service._risk_level(prob, threshold),
            "threshold": threshold,
        },
        "explanation": {
            "base_value": round(baseline_prob, 4),
            "base_logit": bias,
            "contribution_space": "log_odds",
            "impact_pp_is_approximation": True,
            "final_value": round(prob, 4),
            "factors": factors,
            "method": ("LightGBM pred_contrib（精确 SHAP）"
                       if result["engine"] == "lightgbm"
                       else "线性模型精确 Shapley：贡献 = w·z，影响百分点 = 贡献 × p(1-p)"),
        },
        "engine": result["engine"],
        "model_ver": result["model_ver"],
        "features": {k: (None if isinstance(v, float) and math.isnan(v) else v)
                     for k, v in features.iloc[0].to_dict().items()},
        "meta": meta,
    }


def explain_flight(flight_sk: str, top_k: int = 10) -> dict:
    """单航班归因（P7 瀑布图数据）：优先用 DWD 真实特征，再算贡献。"""
    period = None
    df = None
    for month in reversed(engine.BaseEngine.available_periods("dws_flight_features")):
        part = engine.read("dws_flight_features", filters=[("year_month", "=", month)])
        if part.empty:
            continue
        hit = part[part["flight_sk"].astype(str) == flight_sk]
        if not hit.empty:
            df, period = hit, month
            break
    if df is None:
        raise ValueError(f"未找到航班 {flight_sk} 的特征记录")

    features = model_service._feature_frame(df)
    result = model_service.predict_frame(features)
    prob = float(result["prob"][0])
    contrib = result["contrib"].iloc[0]
    factors = model_service.top_factors(contrib, prob, k=top_k)
    row = df.iloc[0]
    threshold = result["threshold"]
    bias = float(result["base_logit"][0])
    base_prob = float(1 / (1 + math.exp(-bias)))
    return {
        "flight_sk": flight_sk,
        "period": period,
        "flight": {
            "flight_date": str(row.get("flight_date")),
            "origin": row.get("origin"), "dest": row.get("dest"),
            "airline": row.get("reporting_airline"),
            "dep_hour": _safe(row.get("dep_hour")),
        },
        "prediction": {
            "prob_delay": round(prob, 4),
            "pred_class": int(prob >= threshold),
            "pred_level": int(model_service._level3_from_threshold(np.array([prob]), threshold)[0]),
            "pred_minutes": float(model_service._pred_minutes(np.array([prob]))[0]),
            "risk_level": model_service._risk_level(prob, threshold),
            "actual_dep_delay_min": _safe(row.get("dep_delay_min")),
            "actual_dep_del15": _safe(row.get("dep_del15")),
        },
        "waterfall": {
            "base_value": round(base_prob, 4),
            "base_logit": bias,
            "contribution_space": "log_odds",
            "impact_pp_is_approximation": True,
            "final_value": round(prob, 4),
            "steps": factors,
        },
        "features": {k: _safe(v) for k, v in row.to_dict().items()},
        "model_ver": result["model_ver"],
        "engine": result["engine"],
    }


def _safe(value):
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    return value


def high_risk_flights(period: str | None = None, top: int = 20,
                      min_prob: float = 0.5) -> dict:
    """高风险航班清单（看板高亮用）。依赖 ads_delay_pred。"""
    period = period or engine.BaseEngine.latest_period("ads_delay_pred")
    if not period:
        raise ValueError("尚无预测结果，请先执行训练与批量预测（POST /api/v1/model/train）")
    df = engine.read("ads_delay_pred", filters=[("year_month", "=", period)])
    if df.empty:
        raise ValueError(f"{period} 无预测结果")
    df["prob_delay"] = pd.to_numeric(df["prob_delay"], errors="coerce")
    hit = df[df["prob_delay"] >= min_prob].sort_values("prob_delay", ascending=False).head(top)
    return {
        "period": period,
        "min_prob": min_prob,
        "total_predicted": int(len(df)),
        "high_risk_rate": round(float((df["prob_delay"] >= min_prob).mean()), 4),
        "items": engine.to_records(hit[[
            "flight_sk", "flight_date", "origin", "dest", "reporting_airline", "dep_hour",
            "prob_delay", "pred_level", "pred_minutes", "risk_level", "top_factors",
            "actual_dep_delay_min", "actual_dep_del15"]]),
    }


def airport_prediction(period: str | None = None, top: int = 20) -> dict:
    """机场级预测：单航班结果聚合（开题报告：机场/航线粒度由单航班结果聚合）。"""
    period = period or engine.BaseEngine.latest_period("ads_delay_pred")
    if not period:
        raise ValueError("尚无预测结果")
    df = engine.read("ads_delay_pred", filters=[("year_month", "=", period)])
    if df.empty:
        raise ValueError(f"{period} 无预测结果")
    df["prob_delay"] = pd.to_numeric(df["prob_delay"], errors="coerce")
    df["pred_class"] = pd.to_numeric(df["pred_class"], errors="coerce")
    df["actual_dep_del15"] = pd.to_numeric(df["actual_dep_del15"], errors="coerce")

    agg = df.groupby("origin", as_index=False).agg(
        flights=("flight_sk", "count"),
        avg_prob=("prob_delay", "mean"),
        pred_delay_cnt=("pred_class", "sum"),
        actual_delay_cnt=("actual_dep_del15", "sum"))
    agg["pred_delay_rate"] = (agg["pred_delay_cnt"] / agg["flights"]).round(4)
    agg["actual_delay_rate"] = (agg["actual_delay_cnt"] / agg["flights"]).round(4)
    agg["abs_error"] = (agg["pred_delay_rate"] - agg["actual_delay_rate"]).abs().round(4)
    agg["avg_prob"] = agg["avg_prob"].round(4)
    agg = agg.sort_values("pred_delay_rate", ascending=False).head(top)
    dim = stats.airport_dim()
    items = []
    for row in agg.to_dict(orient="records"):
        iata = str(row["origin"])
        items.append({"airport_iata": iata, "name": dim.get(iata, {}).get("name", iata), **{
            k: _safe(v) for k, v in row.items() if k != "origin"}})
    return {"period": period, "items": items,
            "note": "机场粒度由单航班预测聚合；actual_delay_rate 为同期真实延误率，可作核验"}


def global_importance(period: str | None = None, top: int = 20) -> dict:
    """全局特征重要度：优先用最近预测的 Top 因子频次，否则回落到基线权重。"""
    period = period or engine.BaseEngine.latest_period("ads_delay_pred")
    counts: dict[str, int] = {}
    if period:
        try:
            df = engine.read("ads_delay_pred", columns=["top_factors"],
                             filters=[("year_month", "=", period)])
            if not df.empty:
                import json
                for text in df["top_factors"].dropna().tolist():
                    try:
                        for name in json.loads(text):
                            counts[name] = counts.get(name, 0) + 1
                    except Exception:
                        continue
        except Exception:
            pass

    total = sum(counts.values())
    if counts:
        items = [{"feature": k, "name_cn": model_service.FEATURE_SPECS.get(k).name_cn
                  if k in model_service.FEATURE_SPECS else k,
                  "count": v, "share": round(v / total, 4)}
                 for k, v in sorted(counts.items(), key=lambda x: -x[1])][:top]
        source = "ads_delay_pred.top_factors"
    else:
        items = [{"feature": k, "name_cn": v.name_cn, "coef": v.coef,
                  "abs_coef": abs(v.coef)}
                 for k, v in sorted(model_service.FEATURE_SPECS.items(),
                                    key=lambda x: -abs(x[1].coef))][:top]
        source = "baseline_weights"
    return {"period": period, "source": source, "items": items}

