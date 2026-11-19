"""看板与查询服务（服务层）：把数仓表加工成前端要的形状。

所有函数都做两件事：分区裁剪（必须带 period 过滤）+ 缓存键由参数决定。
"""
from __future__ import annotations

import math
import re
from functools import lru_cache

import numpy as np
import pandas as pd

from app.config import Config
from app.services import engine
from app.services.cache import json_cached
from app.services.metrics import bucket_delay, compute_metrics, delta_pct, risk_score
from app.utils.logger import get_logger
from app.utils.response import ApiError, ErrCode
from app.utils.timeutil import parse_date

log = get_logger(__name__)

RANK_METRICS = ("delay_rate", "ontime_rate", "cancel_rate", "avg_delay_min")
METRIC_LABELS = {
    "delay_rate": "延误率",
    "ontime_rate": "准点率",
    "cancel_rate": "取消率",
    "avg_delay_min": "平均延误(分钟)",
}


# ---------------------------------------------------------------- 通用
@lru_cache(maxsize=1)
def airport_dim() -> dict[str, dict]:
    """机场维度（IATA → 名称/坐标/时区），供地图与排行展示。"""
    try:
        df = engine.read("dim_airport")
    except ApiError:
        return {}
    if df.empty:
        return {}
    out: dict[str, dict] = {}
    for row in df.to_dict(orient="records"):
        iata = str(row.get("airport_iata") or "").upper()
        if len(iata) != 3:
            continue
        out[iata] = {
            "iata": iata,
            "icao": row.get("icao_code"),
            "name": row.get("name"),
            "city": row.get("city"),
            "country": row.get("country"),
            "lat": _num(row.get("lat")),
            "lon": _num(row.get("lon")),
            "tz": row.get("tz"),
            "runway_count": _num(row.get("runway_count")),
            "is_us": bool(row.get("is_us")),
            "is_china": bool(row.get("is_china")),
        }
    return out


def _num(value):
    if value is None:
        return None
    try:
        f = float(value)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=1)
def airline_dim() -> dict[str, str]:
    try:
        df = engine.read("dim_airline")
    except ApiError:
        return {}
    if df.empty:
        return {}
    return dict(zip(df["airline_code"].astype(str), df["name"].astype(str)))


def latest_period() -> str:
    """看板默认统计期：以 dws_airport_hourly 最新分区为准。"""
    for table in ("dws_airport_hourly", "dwd_flight_detail", "ods_bts_flight"):
        period = engine.BaseEngine.latest_period(table)
        if period:
            return period
    raise ApiError(ErrCode.DATA_NOT_READY, "数仓为空，请先执行数据接入与 ETL")


def period_range(date_from: str | None, date_to: str | None) -> tuple[str, str]:
    """日期 → (起, 止)；缺省回落到最新可用月份的首末日。

    入参一律要求 yyyy-MM-dd（ISO8601），格式不对直接 422，
    避免把 "2015-1-1" 这类写法一路带到 pandas 里炸成 500。
    """
    for name, value in (("date_from", date_from), ("date_to", date_to)):
        if value:
            try:
                parse_date(value)
            except ValueError:
                raise ApiError(ErrCode.PARAM_INVALID, f"{name} 必须是有效的 yyyy-MM-dd 日期，收到 {value!r}")
    if date_from and date_to:
        if str(date_from) > str(date_to):
            raise ApiError(ErrCode.PARAM_INVALID, "date_from 不能晚于 date_to")
        return date_from, date_to
    period = latest_period()
    year, month = int(period[:4]), int(period[5:])
    first = f"{period}-01"
    last_day = int(pd.Period(period, freq="M").days_in_month)
    start, end = date_from or first, date_to or f"{period}-{last_day:02d}"
    if start > end:
        raise ApiError(ErrCode.PARAM_INVALID, "date_from 不能晚于 date_to")
    return start, end


def _months_between(date_from: str, date_to: str) -> list[str]:
    start = pd.Period(date_from[:7], freq="M")
    end = pd.Period(date_to[:7], freq="M")
    if start > end:
        return []
    return [str(p) for p in pd.period_range(start, end, freq="M")]


def _read_periods(table: str, months: list[str], columns=None, filters=None) -> pd.DataFrame:
    if not months:
        raise ApiError(ErrCode.PARAM_INVALID, "时间区间非法：起止日期顺序颠倒")
    frames = []
    for month in months:
        part = engine.read(table, columns=columns,
                           filters=[("year_month", "=", month)] + list(filters or []))
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame(columns=columns or [])
    return pd.concat(frames, ignore_index=True)


def _apply_scope(df: pd.DataFrame, airports: list[str], airlines: list[str],
                 column: str = "airport_iata") -> pd.DataFrame:
    if airports and column in df.columns:
        df = df[df[column].astype(str).isin(airports)]
    if airlines:
        target = "reporting_airline" if "reporting_airline" in df.columns else None
        if target:
            df = df[df[target].astype(str).isin(airlines)]
    return df


# ---------------------------------------------------------------- P1 总览
@json_cached("overview", ttl=Config.CACHE_TTL_DASHBOARD)
def overview_kpi(date_from: str | None = None, date_to: str | None = None,
                 airports: tuple[str, ...] = (), airlines: tuple[str, ...] = ()) -> dict:
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    dws = _read_periods("dws_airport_hourly", months,
                        columns=["airport_iata", "stat_date", "flights", "dep_del15_cnt",
                                 "cancelled_cnt", "dep_delay_sum_min", "severe_cnt"])
    if dws.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"{start}~{end} 无汇总数据")
    dws = dws[(dws["stat_date"].astype(str) >= start) & (dws["stat_date"].astype(str) <= end)]
    dws = _apply_scope(dws, list(airports), list(airlines))

    flights = int(dws["flights"].sum())
    delayed = int(dws["dep_del15_cnt"].sum())
    cancelled = int(dws["cancelled_cnt"].sum())
    operated = flights - cancelled
    delay_sum = float(dws["dep_delay_sum_min"].sum())

    # 环比：与区间等长的前一段
    prev_kpi = _previous_window_kpi(start, end, airports, airlines)

    return {
        "period": {"date_from": start, "date_to": end, "months": months},
        "kpi": {
            "flights": flights,
            "operated": operated,
            "delayed": delayed,
            "cancelled": cancelled,
            "severe_delayed": int(dws["severe_cnt"].sum()),
            "ontime_rate": round((operated - delayed) / operated, 4) if operated else None,
            "delay_rate": round(delayed / operated, 4) if operated else None,
            "cancel_rate": round(cancelled / flights, 4) if flights else None,
            "avg_delay_min": round(delay_sum / operated, 2) if operated else None,
            "airports": int(dws["airport_iata"].nunique()),
        },
        "compared_to_previous": prev_kpi,
        "threshold": {"ontime_min": Config.ONTIME_THRESHOLD_MIN,
                      "severe_min": Config.SEVERE_DELAY_MIN},
        "filter": {"airports": list(airports), "airlines": list(airlines)},
    }


def _previous_window_kpi(start: str, end: str, airports, airlines) -> dict:
    days = (pd.Timestamp(end) - pd.Timestamp(start)).days + 1
    prev_end = (pd.Timestamp(start) - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    prev_start = (pd.Timestamp(start) - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    months = _months_between(prev_start, prev_end)
    try:
        dws = _read_periods("dws_airport_hourly", months,
                            columns=["airport_iata", "stat_date", "flights", "dep_del15_cnt",
                                     "cancelled_cnt", "dep_delay_sum_min"])
        dws = dws[(dws["stat_date"].astype(str) >= prev_start)
                  & (dws["stat_date"].astype(str) <= prev_end)]
        dws = _apply_scope(dws, list(airports), list(airlines))
        if dws.empty:
            return {}
        flights = int(dws["flights"].sum())
        operated = flights - int(dws["cancelled_cnt"].sum())
        delayed = int(dws["dep_del15_cnt"].sum())
        return {
            "window": {"date_from": prev_start, "date_to": prev_end},
            "flights": flights,
            "ontime_rate": round((operated - delayed) / operated, 4) if operated else None,
            "avg_delay_min": round(float(dws["dep_delay_sum_min"].sum()) / operated, 2) if operated else None,
        }
    except Exception:
        return {}


@json_cached("overview", ttl=Config.CACHE_TTL_DASHBOARD)
def overview_trend(date_from: str | None = None, date_to: str | None = None,
                   granularity: str = "day", airports: tuple[str, ...] = (),
                   airlines: tuple[str, ...] = ()) -> dict:
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    dws = _read_periods("dws_airport_hourly", months,
                        columns=["airport_iata", "stat_date", "flights", "dep_del15_cnt",
                                 "cancelled_cnt", "dep_delay_sum_min"])
    dws = dws[(dws["stat_date"].astype(str) >= start) & (dws["stat_date"].astype(str) <= end)]
    dws = _apply_scope(dws, list(airports), list(airlines))
    if dws.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, "区间内无数据")

    dws["bucket"] = (dws["stat_date"].astype(str) if granularity == "day"
                     else dws["stat_date"].astype(str).str.slice(0, 7))
    grouped = dws.groupby("bucket", as_index=False).agg(
        flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
        cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"))
    grouped["operated"] = grouped["flights"] - grouped["cancelled"]
    grouped["ontime_rate"] = ((grouped["operated"] - grouped["delayed"])
                              / grouped["operated"].replace(0, np.nan)).round(4)
    grouped["delay_rate"] = (grouped["delayed"] / grouped["operated"].replace(0, np.nan)).round(4)
    grouped["cancel_rate"] = (grouped["cancelled"] / grouped["flights"].replace(0, np.nan)).round(4)
    grouped["avg_delay_min"] = (grouped["delay_sum"] / grouped["operated"].replace(0, np.nan)).round(2)
    return {
        "granularity": granularity,
        "points": engine.to_records(grouped[["bucket", "flights", "operated", "delayed",
                                             "cancelled", "ontime_rate", "delay_rate",
                                             "cancel_rate", "avg_delay_min"]]),
    }


@json_cached("overview")
def delay_distribution(date_from: str | None = None, date_to: str | None = None,
                       airports: tuple[str, ...] = ()) -> dict:
    """延误时长分布（P1 分布图）。用 DWD 明细分箱，避免预聚合丢失分布形态。"""
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    df = _read_periods("dwd_flight_detail", months, columns=["flight_date", "origin", "dep_delay_min"])
    df = df[(df["flight_date"].astype(str) >= start) & (df["flight_date"].astype(str) <= end)]
    if airports:
        df = df[df["origin"].astype(str).isin(airports)]
    values = pd.to_numeric(df["dep_delay_min"], errors="coerce")
    return {
        "samples": int(values.notna().sum()),
        "buckets": bucket_delay(values),
        "mean": round(float(values.mean()), 2) if values.notna().any() else None,
        "threshold_min": Config.ONTIME_THRESHOLD_MIN,
    }


BTS_DELAY_REASON_LABELS = {
    "carrier_delay": "承运人",
    "weather_delay": "极端天气",
    "nas_delay": "航空运行系统（NAS）",
    "security_delay": "安保",
    "late_aircraft_delay": "前序飞机晚到",
}
BTS_FLIGHT_KEY = ("flight_date", "reporting_airline", "flight_number", "origin")
BTS_ARRIVAL_STATUS = ("arr_del15", "cancelled", "diverted")
BTS_ARRIVAL_DELAY_THRESHOLD_MIN = 15


def _bts_arrival_delayed(df: pd.DataFrame) -> pd.Series:
    status = df.reindex(columns=BTS_ARRIVAL_STATUS).apply(pd.to_numeric, errors="coerce")
    return status["arr_del15"].eq(1) & status["cancelled"].eq(0) & status["diverted"].eq(0)


def _bts_reason_minutes(df: pd.DataFrame) -> pd.DataFrame:
    minutes = df.reindex(columns=Config.DELAY_REASON_COLUMNS).apply(pd.to_numeric, errors="coerce")
    return minutes.where(minutes.gt(0) & np.isfinite(minutes))


@json_cached("overview")
def delay_reasons(date_from: str | None = None, date_to: str | None = None,
                  airports: tuple[str, ...] = (), airlines: tuple[str, ...] = ()) -> dict:
    """汇总 BTS 上报的到港延误原因分钟。"""
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    missing = sorted(set(months) - set(engine.BaseEngine.available_periods("ods_bts_flight")))
    if missing:
        raise ApiError(ErrCode.DATA_NOT_READY, f"当前数仓缺少以下月份的 BTS 原始数据：{', '.join(missing)}")

    columns = [*BTS_FLIGHT_KEY, *BTS_ARRIVAL_STATUS, *Config.DELAY_REASON_COLUMNS]
    filters = [("flight_date", ">=", start), ("flight_date", "<=", end)]
    if airports:
        filters.append(("origin", "in", airports))
    if airlines:
        filters.append(("reporting_airline", "in", airlines))
    minutes_by_reason = dict.fromkeys(Config.DELAY_REASON_COLUMNS, 0.0)
    flights_by_reason = dict.fromkeys(Config.DELAY_REASON_COLUMNS, 0)
    arrival_delayed = reported = 0
    for month in months:
        df = engine.read("ods_bts_flight", columns=columns,
                         filters=[("year_month", "=", month), *filters])
        if df.empty:
            continue
        df = df.drop_duplicates(subset=list(BTS_FLIGHT_KEY))
        eligible = df.loc[_bts_arrival_delayed(df)]
        minutes = _bts_reason_minutes(eligible)
        arrival_delayed += len(eligible)
        reported += int(minutes.notna().any(axis=1).sum())
        for key in Config.DELAY_REASON_COLUMNS:
            minutes_by_reason[key] += float(minutes[key].sum())
            flights_by_reason[key] += int(minutes[key].count())
    total = sum(minutes_by_reason.values())
    items = [
        {"key": key, "label": BTS_DELAY_REASON_LABELS[key],
         "minutes": round(value, 1), "flights": flights_by_reason[key],
         "share": round(value / total, 4) if total else None}
        for key, value in minutes_by_reason.items()
    ]
    return {
        "period": months[-1],
        "date_from": start,
        "date_to": end,
        "items": items,
        "arrival_delayed_flights": arrival_delayed,
        "reported_flights": reported,
        "total_minutes": round(total, 1),
        "threshold_min": BTS_ARRIVAL_DELAY_THRESHOLD_MIN,
        "note": "占比按 BTS 已上报的原因分钟计算。同一航班可以有多项原因。",
    }


# ---------------------------------------------------------------- P2 网络
@json_cached("network", ttl=Config.CACHE_TTL_DASHBOARD)
def network_graph(date_from: str | None = None, date_to: str | None = None,
                  metric: str = "delay_rate", top_routes: int = 300,
                  min_flights: int = 1, airports: tuple[str, ...] = ()) -> dict:
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    nodes_df = _read_periods("dws_airport_hourly", months,
                             columns=["airport_iata", "stat_date", "flights", "dep_del15_cnt",
                                      "cancelled_cnt", "dep_delay_sum_min"])
    nodes_df = nodes_df[(nodes_df["stat_date"].astype(str) >= start)
                        & (nodes_df["stat_date"].astype(str) <= end)]
    if nodes_df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"{start}~{end} 无网络数据")
    agg = nodes_df.groupby("airport_iata", as_index=False).agg(
        flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
        cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"))
    agg["operated"] = agg["flights"] - agg["cancelled"]
    agg["delay_rate"] = (agg["delayed"] / agg["operated"].replace(0, np.nan)).round(4)
    agg["ontime_rate"] = ((agg["operated"] - agg["delayed"]) / agg["operated"].replace(0, np.nan)).round(4)
    agg["avg_delay_min"] = (agg["delay_sum"] / agg["operated"].replace(0, np.nan)).round(2)
    agg = agg[agg["flights"] >= min_flights]

    dim = airport_dim()
    nodes = []
    for row in agg.to_dict(orient="records"):
        iata = str(row["airport_iata"])
        info = dim.get(iata, {})
        nodes.append({
            "id": iata,
            "name": info.get("name") or iata,
            "city": info.get("city"),
            "country": info.get("country"),
            "lat": info.get("lat"),
            "lon": info.get("lon"),
            "tz": info.get("tz"),
            "flights": int(row["flights"]),
            "operated": int(row["operated"]),
            "delayed": int(row["delayed"]),
            "delay_rate": _num(row["delay_rate"]),
            "ontime_rate": _num(row["ontime_rate"]),
            "cancel_rate": _num((row["cancelled"] / row["flights"]) if row["flights"] else None),
            "avg_delay_min": _num(row["avg_delay_min"]),
            "value": _num(row.get(metric, row["delay_rate"])),
        })
    nodes.sort(key=lambda n: n["flights"], reverse=True)

    routes_df = _read_periods("dws_route_daily", months,
                              columns=["origin", "dest", "route_key", "stat_date", "flights", "dep_del15_cnt",
                                       "cancelled_cnt", "dep_delay_sum_min", "distance_mi"])
    if not routes_df.empty:
        routes_df = routes_df[(routes_df["stat_date"].astype(str) >= start)
                              & (routes_df["stat_date"].astype(str) <= end)]
        # 统一为出港口径；机场筛选在 Top N 截断前应用，避免遗漏低流量航线。
        if airports:
            routes_df = routes_df[routes_df["origin"].astype(str).isin(airports)]
    links = []
    if not routes_df.empty:
        route_agg = routes_df.groupby(["route_key", "origin", "dest"], as_index=False).agg(
            flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
            cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"),
            distance_mi=("distance_mi", "median"))
        route_agg["operated"] = route_agg["flights"] - route_agg["cancelled"]
        route_agg["delay_rate"] = (route_agg["delayed"] / route_agg["operated"].replace(0, np.nan)).round(4)
        route_agg["avg_delay_min"] = (route_agg["delay_sum"] / route_agg["operated"].replace(0, np.nan)).round(2)
        route_agg = route_agg.sort_values("flights", ascending=False).head(top_routes)
        node_ids = {n["id"] for n in nodes}
        for row in route_agg.to_dict(orient="records"):
            if row["origin"] not in node_ids or row["dest"] not in node_ids:
                continue
            src, dst = dim.get(row["origin"], {}), dim.get(row["dest"], {})
            links.append({
                "source": row["origin"], "target": row["dest"],
                "route_key": row["route_key"],
                "flights": int(row["flights"]),
                "delay_rate": _num(row["delay_rate"]),
                "avg_delay_min": _num(row["avg_delay_min"]),
                "distance_mi": _num(row["distance_mi"]),
                "coords": [[src.get("lon"), src.get("lat")], [dst.get("lon"), dst.get("lat")]],
            })

    scope_nodes = [n for n in nodes if not airports or n["id"] in airports]
    return {
        "period": {"date_from": start, "date_to": end},
        "metric": metric,
        "filter": {"airports": list(airports), "direction": "departure"},
        "nodes": nodes,
        "links": links,
        "summary": {
            "airports": len(scope_nodes),
            "routes": len(links),
            "flights": sum(n["flights"] for n in scope_nodes),
            "avg_delay_rate": (round(float(np.mean([n["delay_rate"] for n in scope_nodes
                                                    if n["delay_rate"] is not None])), 4)
                               if any(n["delay_rate"] is not None for n in scope_nodes) else None),
        },
        "basemap": {"china_domain": "见 /api/v1/crossdomain/china"},
    }


@json_cached("network", ttl=Config.CACHE_TTL_DASHBOARD)
def delay_heatmap(date_from: str | None = None, date_to: str | None = None,
                  top: int = 20, metric: str = "delay_rate") -> dict:
    """机场×小时热力图（P2/P1 共用）。"""
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    df = _read_periods("dws_airport_hourly", months,
                       columns=["airport_iata", "stat_hour", "flights", "dep_del15_cnt",
                                "cancelled_cnt", "dep_delay_sum_min"])
    if df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, "无热力图数据")
    top_airports = (df.groupby("airport_iata")["flights"].sum()
                    .sort_values(ascending=False).head(top).index.tolist())
    df = df[df["airport_iata"].isin(top_airports)]
    agg = df.groupby(["airport_iata", "stat_hour"], as_index=False).agg(
        flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
        cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"))
    agg["operated"] = agg["flights"] - agg["cancelled"]
    if metric == "avg_delay_min":
        agg["value"] = agg["delay_sum"] / agg["operated"].replace(0, np.nan)
    elif metric == "cancel_rate":
        agg["value"] = agg["cancelled"] / agg["flights"].replace(0, np.nan)
    elif metric == "flights":
        agg["value"] = agg["flights"]
    else:
        agg["value"] = agg["delayed"] / agg["operated"].replace(0, np.nan)

    dim = airport_dim()
    matrix = [[None] * 24 for _ in top_airports]
    index = {iata: i for i, iata in enumerate(top_airports)}
    for row in agg.to_dict(orient="records"):
        i = index.get(row["airport_iata"])
        hour = int(row["stat_hour"]) if row["stat_hour"] is not None else None
        if i is None or hour is None or not (0 <= hour < 24):
            continue
        matrix[i][hour] = _num(round(float(row["value"]), 4) if row["value"] is not None else None)
    return {
        "metric": metric,
        "hours": list(range(24)),
        "airports": [{"id": iata, "name": dim.get(iata, {}).get("name", iata)} for iata in top_airports],
        "matrix": matrix,
        "period": {"date_from": start, "date_to": end},
    }


# ---------------------------------------------------------------- P3 排行
@json_cached("airport", ttl=Config.CACHE_TTL_DASHBOARD)
def airport_rank(stat_date: str | None = None, metric: str = "delay_rate", top: int = 20,
                 order: str = "desc") -> dict:
    if metric not in RANK_METRICS:
        raise ApiError(ErrCode.PARAM_INVALID, f"metric 只能是 {RANK_METRICS}")
    period = stat_date[:7] if stat_date else latest_period()
    try:
        df = engine.read("ads_airport_rank", filters=[("year_month", "=", period)])
    except ApiError:
        raise ApiError(ErrCode.DATA_NOT_READY, "排行表为空，请先执行 ETL（ads_airport_rank）")
    if df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"{period} 无排行数据")
    df = df[df["metric"] == metric]
    if stat_date:
        df = df[df["stat_date"].astype(str) == stat_date]
    else:
        latest = df["stat_date"].astype(str).max()
        df = df[df["stat_date"].astype(str) == latest]
    df = df.sort_values("value", ascending=(order == "asc")).head(top)

    dim = airport_dim()
    items = []
    for row in df.to_dict(orient="records"):
        iata = str(row["airport_iata"])
        items.append({
            "rank": int(row["rank_no"]) if row.get("rank_no") is not None else None,
            "airport_iata": iata,
            "name": dim.get(iata, {}).get("name", iata),
            "city": dim.get(iata, {}).get("city"),
            "value": _num(row["value"]),
            "delta_pct": _num(row.get("delta_pct")),
            "flights": int(row["flights"]) if row.get("flights") is not None else None,
            "stat_date": row.get("stat_date"),
        })
    return {"metric": metric, "metric_label": METRIC_LABELS.get(metric, metric),
            "order": order, "items": items}


@json_cached("airport", ttl=Config.CACHE_TTL_DASHBOARD)
def airport_profile(iata: str, date_from: str | None = None, date_to: str | None = None) -> dict:
    """机场详情：24 小时延误曲线 + 气象叠加 + 主要航线（P3 下钻）。"""
    iata = iata.upper()
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    df = _read_periods("dws_airport_hourly", months, filters=[("airport_iata", "=", iata)])
    if df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"机场 {iata} 在 {start}~{end} 无数据")
    df = df[(df["stat_date"].astype(str) >= start) & (df["stat_date"].astype(str) <= end)]

    hourly = df.groupby("stat_hour", as_index=False).agg(
        flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
        cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"),
        vis=("vis_mean_mi", "mean"), wind=("wind_max_ms", "mean"),
        tmp=("tmp_mean_c", "mean"), precip=("precip_sum_mm", "sum"))
    hourly["operated"] = hourly["flights"] - hourly["cancelled"]
    hourly["delay_rate"] = (hourly["delayed"] / hourly["operated"].replace(0, np.nan)).round(4)
    hourly["avg_delay_min"] = (hourly["delay_sum"] / hourly["operated"].replace(0, np.nan)).round(2)

    daily = df.groupby("stat_date", as_index=False).agg(
        flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
        cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"))
    daily["operated"] = daily["flights"] - daily["cancelled"]
    daily["delay_rate"] = (daily["delayed"] / daily["operated"].replace(0, np.nan)).round(4)
    daily["avg_delay_min"] = (daily["delay_sum"] / daily["operated"].replace(0, np.nan)).round(2)

    routes = engine.read("dws_route_daily",
                         columns=["route_key", "origin", "dest", "flights", "dep_del15_cnt",
                                  "cancelled_cnt", "dep_delay_sum_min"],
                         filters=[("year_month", "=", months[-1])])
    routes = routes[(routes["origin"].astype(str) == iata) | (routes["dest"].astype(str) == iata)]
    top_routes = []
    if not routes.empty:
        grouped = routes.groupby(["route_key", "origin", "dest"], as_index=False).agg(
            flights=("flights", "sum"), delayed=("dep_del15_cnt", "sum"),
            cancelled=("cancelled_cnt", "sum"), delay_sum=("dep_delay_sum_min", "sum"))
        grouped["operated"] = grouped["flights"] - grouped["cancelled"]
        grouped["delay_rate"] = (grouped["delayed"] / grouped["operated"].replace(0, np.nan)).round(4)
        grouped["avg_delay_min"] = (grouped["delay_sum"] / grouped["operated"].replace(0, np.nan)).round(2)
        top_routes = engine.to_records(grouped.sort_values("flights", ascending=False)
                                       .head(10)[["route_key", "origin", "dest", "flights",
                                                  "delay_rate", "avg_delay_min"]])

    info = airport_dim().get(iata, {})
    return {
        "airport": {"iata": iata, **info},
        "period": {"date_from": start, "date_to": end},
        "kpi": {
            "flights": int(df["flights"].sum()),
            "delayed": int(df["dep_del15_cnt"].sum()),
            "cancelled": int(df["cancelled_cnt"].sum()),
            "ontime_rate": _num(round((df["flights"].sum() - df["cancelled_cnt"].sum()
                                       - df["dep_del15_cnt"].sum())
                                      / max(1, df["flights"].sum() - df["cancelled_cnt"].sum()), 4)),
            "avg_delay_min": _num(round(float(df["dep_delay_sum_min"].sum())
                                        / max(1, df["flights"].sum() - df["cancelled_cnt"].sum()), 2)),
        },
        "hourly": engine.to_records(hourly[["stat_hour", "flights", "delay_rate",
                                            "avg_delay_min", "vis", "wind", "tmp", "precip"]]),
        "daily": engine.to_records(daily[["stat_date", "flights", "delay_rate", "avg_delay_min"]]),
        "top_routes": top_routes,
    }


# ---------------------------------------------------------------- P4 传播路径
@json_cached("airport")
def propagation(stat_date: str | None = None, airport: str | None = None,
                limit: int = 2000) -> dict:
    """延误传播：前序航班延误 → 本航班延误 的桑基图数据（§5.3 航班链）。"""
    period = stat_date[:7] if stat_date else latest_period()
    df = engine.read("dwd_flight_detail",
                     columns=["flight_sk", "flight_date", "origin", "dest", "reporting_airline",
                              "prev_arr_delay_min", "dep_delay_min", "turnaround_min",
                              "buffer_ratio", "risk_flag", "chain_available"],
                     filters=[("year_month", "=", period)])
    if df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"{period} 无明细数据")
    df = df[df["chain_available"] == 1]
    if stat_date:
        df = df[df["flight_date"].astype(str) == stat_date]
    if airport:
        df = df[df["origin"].astype(str) == airport.upper()]
    df = df.dropna(subset=["prev_arr_delay_min", "dep_delay_min"])
    if df.empty:
        raise ApiError(ErrCode.EMPTY_RESULT, "无可用航班链样本")

    def _bucket(v):
        if v < Config.ONTIME_THRESHOLD_MIN:
            return "前序准点" if v is not None else ""
        if v <= Config.SEVERE_DELAY_MIN:
            return "前序轻微延误"
        return "前序严重延误"

    df = df.assign(prev_bucket=[_bucket(v) for v in df["prev_arr_delay_min"]],
                   cur_bucket=[_bucket(v) for v in df["dep_delay_min"]])
    links = (df.groupby(["prev_bucket", "cur_bucket"], as_index=False)
             .agg(value=("flight_sk", "count"),
                  avg_delay=("dep_delay_min", "mean"),
                  avg_turnaround=("turnaround_min", "mean"),
                  risk_cnt=("risk_flag", "sum")))
    links["avg_delay"] = links["avg_delay"].round(1)
    links["avg_turnaround"] = links["avg_turnaround"].round(1)

    propagation_rate = float((df["prev_arr_delay_min"] >= Config.ONTIME_THRESHOLD_MIN).mean())
    inherited = float(((df["prev_arr_delay_min"] >= Config.ONTIME_THRESHOLD_MIN)
                       & (df["dep_delay_min"] >= Config.ONTIME_THRESHOLD_MIN)).mean())
    return {
        "period": period,
        "stat_date": stat_date,
        "airport": airport,
        "nodes": ["前序准点", "前序轻微延误", "前序严重延误",
                  "本班准点", "本班轻微延误", "本班严重延误"],
        "links": engine.to_records(links),
        "summary": {
            "samples": int(len(df)),
            "chain_available_rate": round(float(len(df) / max(1, limit)), 4),
            "prev_delayed_share": round(propagation_rate, 4),
            "inherited_delay_rate": round(inherited, 4),
            "risk_flag_share": round(float(df["risk_flag"].fillna(0).mean()), 4),
        },
        "samples": engine.to_records(
            df.sort_values("dep_delay_min", ascending=False).head(20)[
                ["flight_sk", "flight_date", "origin", "dest", "reporting_airline",
                 "prev_arr_delay_min", "dep_delay_min", "turnaround_min", "buffer_ratio"]]),
    }


# ---------------------------------------------------------------- 查询与导出
def flights_query(date_from: str | None = None, date_to: str | None = None,
                  origin: str | None = None, dest: str | None = None,
                  airline: str | None = None, min_delay: float | None = None,
                  max_delay: float | None = None, cancelled: bool | None = None,
                  page: int = 1, page_size: int = 20, sort_by: str = "dep_delay_min",
                  order: str = "desc", limit: int | None = None) -> dict:
    columns = ["flight_sk", "flight_date", "reporting_airline", "flight_number", "origin",
               "dest", "crs_dep_hhmm", "dep_hhmm", "dep_delay_min", "arr_delay_min",
               "dep_del15", "delay_level", "cancelled", "cancellation_code", "distance_mi",
               "prev_arr_delay_min", "turnaround_min", "buffer_ratio", "vis_min_mi",
               "wind_max_ms", "weather_class", "src_file"]
    start, end = period_range(date_from, date_to)
    months = _months_between(start, end)
    frames = []
    for month in months:
        filters = [("year_month", "=", month), ("flight_date", ">=", start), ("flight_date", "<=", end)]
        if origin:
            filters.append(("origin", "=", origin.upper()))
        if dest:
            filters.append(("dest", "=", dest.upper()))
        if airline:
            filters.append(("reporting_airline", "=", airline.upper()))
        part = engine.read("dwd_flight_detail", columns=columns, filters=filters)
        if not part.empty:
            frames.append(part)
    if not frames:
        raise ApiError(ErrCode.EMPTY_RESULT, f"{start}~{end} 无符合条件的航班")
    df = pd.concat(frames, ignore_index=True)
    df = df[(df["flight_date"].astype(str) >= start) & (df["flight_date"].astype(str) <= end)]
    df["dep_delay_min"] = pd.to_numeric(df["dep_delay_min"], errors="coerce")
    if min_delay is not None:
        df = df[df["dep_delay_min"] >= min_delay]
    if max_delay is not None:
        df = df[df["dep_delay_min"] <= max_delay]
    if cancelled is not None:
        df = df[(pd.to_numeric(df["cancelled"], errors="coerce") == 1) == cancelled]

    total = int(len(df))
    if sort_by in df.columns:
        df = df.sort_values([sort_by, "flight_sk"], ascending=[order == "asc", True], na_position="last")
    if limit:
        df = df.head(limit)
        page, page_size = 1, len(df)
    offset = (page - 1) * page_size
    sliced = df.iloc[offset:offset + page_size]
    keep = [c for c in columns if c in sliced.columns]
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": engine.to_records(sliced[keep]),
        "filters": {
            "date_from": start, "date_to": end, "origin": origin, "dest": dest,
            "airline": airline, "min_delay": min_delay, "max_delay": max_delay,
            "cancelled": cancelled,
        },
    }


def _flight_bts_reasons(flight: dict, month: str) -> dict:
    result = {"status": "not_reported", "items": []}
    arrival_delay = flight.get("arr_delay_min")
    if (flight.get("cancelled") == 1 or flight.get("diverted") == 1
            or flight.get("arr_del15") == 0
            or (arrival_delay is not None and arrival_delay < BTS_ARRIVAL_DELAY_THRESHOLD_MIN)):
        return {**result, "status": "not_applicable"}
    fields = (*BTS_FLIGHT_KEY, "dest")
    if (any(not flight.get(field) for field in fields)
            or month not in engine.BaseEngine.available_periods("ods_bts_flight")):
        return {**result, "status": "source_unavailable"}
    filters = [("year_month", "=", month), *((field, "=", flight[field]) for field in fields)]
    try:
        raw = engine.read("ods_bts_flight", columns=[*BTS_ARRIVAL_STATUS, *Config.DELAY_REASON_COLUMNS],
                          filters=filters, limit=1)
    except ApiError as exc:
        if exc.code != ErrCode.DATA_NOT_READY:
            raise
        return {**result, "status": "source_unavailable"}
    if raw.empty:
        return {**result, "status": "source_unavailable"}
    eligible = raw.loc[_bts_arrival_delayed(raw)]
    if eligible.empty:
        return result
    minutes = _bts_reason_minutes(eligible).iloc[0].dropna().sort_values(ascending=False, kind="stable")
    if minutes.empty:
        return result
    return {
        "status": "reported",
        "items": [{"key": key, "label": BTS_DELAY_REASON_LABELS[key], "minutes": float(value)}
                  for key, value in minutes.items()],
    }


def flight_detail(flight_sk: str, flight_date: str | None = None) -> dict:
    months = engine.BaseEngine.available_periods("dwd_flight_detail")
    if flight_date:
        months = [month for month in months if month == flight_date[:7]]
    filters = [("flight_sk", "=", flight_sk)]
    if flight_date:
        filters.append(("flight_date", "=", flight_date))
    for month in reversed(months):
        df = engine.read("dwd_flight_detail", filters=[("year_month", "=", month), *filters], limit=1)
        if df.empty:
            continue
        hit = df[df["flight_sk"].astype(str) == flight_sk]
        if not hit.empty:
            flight = engine.to_records(hit)[0]
            flight["arrival_delay_reasons"] = _flight_bts_reasons(flight, month)
            return flight
    raise ApiError(ErrCode.NOT_FOUND, f"未找到航班：{flight_sk}")


# ---------------------------------------------------------------- 质量报告
@json_cached("quality")
def quality_report(period: str | None = None) -> dict:
    """清洗前后行数、剔除原因分布、重复率（FR-7 数据质量监控）。"""
    from app.extensions import db
    from app.models import IngestAudit, JobRun

    period = period or latest_period()
    ods_rows = 0
    dwd_rows = 0
    try:
        ods_rows = len(engine.read("ods_bts_flight", columns=["flight_date"],
                                  filters=[("year_month", "=", period)]))
    except ApiError:
        pass
    try:
        dwd_rows = len(engine.read("dwd_flight_detail", columns=["flight_sk"],
                                  filters=[("year_month", "=", period)]))
    except ApiError:
        pass

    audits = (db.session.query(IngestAudit).filter(IngestAudit.period == period)
              .order_by(IngestAudit.id.desc()).limit(50).all())
    jobs = (db.session.query(JobRun).filter(JobRun.period.like(f"%{period}%"))
            .order_by(JobRun.id.desc()).limit(20).all())
    drop_reasons: dict[str, int] = {}
    flag_reasons: dict[str, int] = {}
    for job in jobs:
        payload = job.to_dict().get("drop_reason") or {}
        if not isinstance(payload, dict):
            continue
        groups = {"drops": payload, "flags": {}} if not any(
            k in ("drops", "flags") for k in payload) else payload
        for key, value in (groups.get("drops") or {}).items():
            if isinstance(value, (int, float)):
                drop_reasons[key] = drop_reasons.get(key, 0) + int(value)
        for key, value in (groups.get("flags") or {}).items():
            if isinstance(value, (int, float)):
                flag_reasons[key] = flag_reasons.get(key, 0) + int(value)

    dup_rate = None
    if ods_rows:
        dup_rate = round(max(0.0, (ods_rows - dwd_rows) / ods_rows), 4)
    return {
        "period": period,
        "rows": {"ods": ods_rows, "dwd": dwd_rows,
                 "kept_rate": round(dwd_rows / ods_rows, 4) if ods_rows else None},
        "drop_reasons": drop_reasons,
        "flag_reasons": flag_reasons,
        "duplicate_rate": dup_rate,
        "thresholds": {"duplicate_rate_max": 0.005, "kept_rate_min": 0.995},
        "gates": {
            "file_level": "MD5 + 字节数 + 周期完整（/api/v1/ingest/verify）",
            "field_level": "Origin/Dest 三位大写、FlightDate 在 2015–2024、DepDelay ∈ [-300,1500]",
            "row_level": "唯一键去重 + 时间逻辑校验",
        },
        "audits": [a.to_dict() for a in audits],
    }


# ---------------------------------------------------------------- 跨域（中国域）
@lru_cache(maxsize=4)
def _load_stpn(china: bool = True) -> dict:
    """读取 STPN npy（delay / weather / 距离矩阵 / 机场列表）。"""
    from pathlib import Path
    root = Path(Config.DATA_ROOT) / "07_STPN中美航班数据集" / ("cdata" if china else "udata")
    try:
        import numpy as np
        delay = np.load(root / ("delay.npy" if china else "udelay.npy"))
        weather = np.load(root / ("weather_cn.npy" if china else "weather2016_2021.npy"))
        airports = [line.strip() for line in
                    (root / ("CHina.csv" if china else "USA.csv")).read_text(encoding="utf-8").splitlines()
                    if line.strip()]
        return {"delay": delay, "weather": weather, "airports": airports, "period": 36 if china else 24}
    except Exception as exc:
        raise ApiError(ErrCode.DATA_NOT_READY, f"STPN 数据不可用：{exc}")


@json_cached("network")
def crossdomain_china(top: int = 50, date_from: str | None = None,
                      date_to: str | None = None) -> dict:
    """中国域：机场 × 30 分钟延误指标（2015-04-01 ~ 2017-05-01）。"""
    data = _load_stpn(china=True)
    delay = data["delay"]           # (机场, 时间步, 2)
    weather = data["weather"]       # (机场, 时间步)
    airports = data["airports"]
    periods_per_day = 36
    days = delay.shape[1] // periods_per_day

    dep_delay = delay[:, :, 1]
    thr = Config.ONTIME_THRESHOLD_MIN
    daily = dep_delay.reshape(delay.shape[0], days, periods_per_day)
    # STPN 里缺测是 NaN：必须用 nan 系列聚合，否则一天里只要有一个空时段整天就变 NaN
    daily_mean = np.nanmean(daily, axis=2)
    delayed_flag = np.where(np.isnan(daily), np.nan, (daily >= thr).astype("float64"))
    daily_delayed = np.nanmean(delayed_flag, axis=2)
    weather_daily = np.nanmean(weather.reshape(weather.shape[0], days, periods_per_day), axis=2)

    start_day, end_day = _china_day_window(date_from, date_to, days)
    mean_delay = np.nanmean(daily_mean[:, start_day:end_day], axis=1)
    delay_rate = np.nanmean(daily_delayed[:, start_day:end_day], axis=1)
    wx = np.nanmean(weather_daily[:, start_day:end_day], axis=1)

    items = []
    for i, iata in enumerate(airports):
        if i >= mean_delay.shape[0]:
            break
        items.append({
            "airport_iata": iata,
            "avg_dep_delay_min": _num(round(float(mean_delay[i]), 2)) if not np.isnan(mean_delay[i]) else None,
            "delay_rate": _num(round(float(delay_rate[i]), 4)) if not np.isnan(delay_rate[i]) else None,
            "weather_class_mean": _num(round(float(wx[i]), 2)) if not np.isnan(wx[i]) else None,
        })
    items.sort(key=lambda x: (x["delay_rate"] is None, -(x["delay_rate"] or 0)))
    return {
        "domain": "CN",
        "granularity": "airport × 30min",
        "source": "STPN (IEEE DataPort DOI 10.21227/4e59-r355)",
        "time_span": {"available_days": days, "window": [start_day, end_day],
                      "note": "2015-04-01 ~ 2017-05-01，每天 36 个时段（06:00–23:59）"},
        "airports": len(airports),
        "items": items[:top],
        "weather_classes": {"0": "normal", "1": "rain", "2": "cloud", "3": "thunderstorm",
                            "4": "fog", "5": "storm", "6": "snow"},
    }


def _china_day_window(date_from: str | None, date_to: str | None, days: int) -> tuple[int, int]:
    base = pd.Timestamp("2015-04-01")
    if not date_from:
        return 0, days
    try:
        start = max(0, (pd.Timestamp(date_from) - base).days)
        end = min(days, (pd.Timestamp(date_to or date_from) - base).days + 1) if date_to else start + 1
        return start, max(start + 1, end)
    except Exception:
        return 0, days


@json_cached("network")
def crossdomain_compare(period: str | None = None) -> dict:
    """美国域（BTS 聚合到机场×30 分钟）vs 中国域（STPN）的指标对照。"""
    period = period or latest_period()
    df = engine.read("dws_delay_network", filters=[("year", "=", period[:4])])
    df = df[df["slot_id"].astype(str).str.startswith(period)]
    us = {
        "airports": int(df["airport_iata"].nunique()) if not df.empty else 0,
        "slots": int(len(df)),
        "avg_flights_per_slot": _num(round(float(df["flights"].mean()), 2)) if not df.empty else None,
        "ontime_rate": _num(round(float((df["flights"].sum() - df["dep_del15_cnt"].sum())
                                        / max(1, df["flights"].sum())), 4)) if not df.empty else None,
        "avg_delay_min": _num(round(float(df["dep_delay_avg_min"].mean()), 2)) if not df.empty else None,
    }
    data = _load_stpn(china=True)
    dep = data["delay"][:, :, 1]
    valid = ~np.isnan(dep)
    thr = Config.ONTIME_THRESHOLD_MIN
    cn = {
        "airports": len(data["airports"]),
        "slots": int(dep.shape[1]),
        "valid_slots": int(valid.sum()),
        # 只在有效时段上算准点率，NaN 不能当作"延误"（否则中国域准点率被低估一倍）
        "ontime_rate": _num(round(float((dep[valid] < thr).mean()), 4)) if valid.any() else None,
        "avg_delay_min": _num(round(float(np.nanmean(dep)), 2)),
        "delay_rate": _num(round(float((dep[valid] >= thr).mean()), 4)) if valid.any() else None,
    }
    return {
        "period": period,
        "us_domain": us,
        "cn_domain": cn,
        "alignment": {
            "granularity": "airport × 30min（S-09 跨域特征同构）",
            "caveat": "中国无逐航班明细，仅能做机场/时段级指标对照；单航班预测仅美国域",
        },
    }

