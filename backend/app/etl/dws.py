"""DWS 汇总层作业：机场×小时 / 航线×天 / 机场×30 分钟时间片 / 建模特征宽表。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import Config
from app.etl.common import JobContext, to_num
from app.services import engine
from app.services.catalog import MODEL_FEATURES
from app.services.metrics import risk_score
from app.utils.logger import get_logger
from app.utils.timeutil import prev_month

log = get_logger(__name__)

DWD_COLUMNS = [
    "flight_sk", "flight_date", "year_month", "day_of_week", "dep_hour", "dep_slot",
    "reporting_airline", "tail_number", "flight_number", "origin", "dest", "route_key",
    "dep_delay_min", "dep_del15", "arr_delay_min", "delay_level", "cancelled",
    "distance_mi", "crs_elapsed_min", "chain_available", "prev_arr_delay_min",
    "turnaround_min", "buffer_ratio", "risk_flag", "weather_available",
    "vis_min_mi", "wind_max_ms", "tmp_mean_c", "precip_mm", "weather_class",
]


def _load_dwd(period: str) -> pd.DataFrame:
    df = engine.read("dwd_flight_detail", columns=DWD_COLUMNS,
                     filters=[("year_month", "=", period)])
    if df.empty:
        raise ValueError(f"{period} 的 dwd_flight_detail 为空，请先跑 DWD 作业")
    for col in ("dep_delay_min", "arr_delay_min", "distance_mi", "crs_elapsed_min",
                "prev_arr_delay_min", "turnaround_min", "buffer_ratio",
                "vis_min_mi", "wind_max_ms", "tmp_mean_c", "precip_mm"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("dep_del15", "cancelled", "dep_hour", "dep_slot", "delay_level", "chain_available"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _flags(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["_del15"] = (df["dep_del15"].fillna(0) >= 1).astype(int)
    df["_severe"] = (df["delay_level"] == 2).astype(int)
    df["_cancel"] = (df["cancelled"] == 1).astype(int)
    df["_oper"] = 1 - df["_cancel"]
    df["_delay"] = df["dep_delay_min"].where(df["_oper"] == 1)
    df["_chain"] = (df["chain_available"] == 1).astype(int)
    return df


def _rates(frame: pd.DataFrame) -> pd.DataFrame:
    """统一算三率；调用前须把 _del15/_cancel 重命名成正式列名。"""
    frame = frame.copy()
    operated = frame["_operated"].replace(0, np.nan)
    frame["ontime_rate"] = ((frame["_operated"] - frame["dep_del15_cnt"]) / operated).round(4)
    frame["delay_rate"] = (frame["dep_del15_cnt"] / operated).round(4)
    frame["cancel_rate"] = (frame["cancelled_cnt"] / frame["flights"].replace(0, np.nan)).round(4)
    frame = frame.drop(columns=[c for c in ("_operated", "_oper", "_del15", "_cancel", "_severe",
                                            "_chain", "_prev15") if c in frame.columns],
                       errors="ignore")
    return frame


# ---------------------------------------------------------------- 机场×小时
def build_dws_airport_hourly(period: str, operator: str = "system") -> dict:
    ctx = JobContext("dws_airport_hourly", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        df = _flags(_load_dwd(period))
        ctx.rows_in = len(df)
        ctx.stage_start("aggregate")
        out = (df.groupby(["origin", "flight_date", "dep_hour"], as_index=False)
               .agg(flights=("flight_sk", "count"),
                    _del15=("_del15", "sum"), _severe=("_severe", "sum"),
                    _cancel=("_cancel", "sum"), _oper=("_oper", "sum"),
                    dep_delay_sum_min=("dep_delay_min", "sum"),
                    dep_delay_avg_min=("dep_delay_min", "mean"),
                    p90=("_delay", lambda s: s.quantile(0.90) if s.notna().any() else np.nan),
                    weather_available_cnt=("weather_available", "sum"),
                    vis_mean_mi=("vis_min_mi", "mean"),
                    wind_max_ms=("wind_max_ms", "max"),
                    tmp_mean_c=("tmp_mean_c", "mean"),
                    precip_sum_mm=("precip_mm", "sum")))
        out = out.rename(columns={"origin": "airport_iata", "p90": "p90_dep_delay_min",
                                  "_del15": "dep_del15_cnt", "_severe": "severe_cnt",
                                  "_cancel": "cancelled_cnt", "_oper": "_operated"})
        out = _rates(out)
        out["year_month"] = period
        out["stat_date"] = out["flight_date"]
        out["dep_delay_sum_min"] = out["dep_delay_sum_min"].round(1)
        out["dep_delay_avg_min"] = out["dep_delay_avg_min"].round(2)
        out["p90_dep_delay_min"] = out["p90_dep_delay_min"].round(1)
        for col in ("vis_mean_mi", "tmp_mean_c", "precip_sum_mm"):
            out[col] = out[col].round(2)
        out["cancel_rate"] = out["cancel_rate"].fillna(0.0)
        ctx.stage_end("aggregate", f"{len(out)} 个机场×小时")

        result = engine.write("dws_airport_hourly", out, partition_value=period, mode="overwrite")
        ctx.rows_out = result["rows"]
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWS 机场小时作业失败：%s", exc)
        return ctx.finish("failed", "E_DWS_AIRPORT", str(exc))


# ---------------------------------------------------------------- 航线×天
def build_dws_route_daily(period: str, operator: str = "system") -> dict:
    ctx = JobContext("dws_route_daily", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        df = _flags(_load_dwd(period))
        df["_prev15"] = (df["prev_arr_delay_min"].fillna(-999) >= Config.ONTIME_THRESHOLD_MIN).astype(int)
        ctx.rows_in = len(df)
        ctx.stage_start("aggregate")
        grouped = df.groupby(["route_key", "flight_date"], as_index=False)
        out = grouped.agg(
            origin=("origin", "first"), dest=("dest", "first"),
            flights=("flight_sk", "count"), airlines=("reporting_airline", "nunique"),
            _del15=("_del15", "sum"), _cancel=("_cancel", "sum"), _oper=("_oper", "sum"),
            dep_delay_sum_min=("dep_delay_min", "sum"),
            dep_delay_avg_min=("dep_delay_min", "mean"),
            distance_mi=("distance_mi", "median"),
            prev_delay_cnt=("_prev15", "sum"),
            chain_available_cnt=("_chain", "sum"),
        )
        out = out.rename(columns={"_del15": "dep_del15_cnt", "_cancel": "cancelled_cnt",
                                  "_oper": "_operated"})
        out = _rates(out)
        out["year_month"] = period
        out["stat_date"] = out["flight_date"]
        out["dep_delay_sum_min"] = out["dep_delay_sum_min"].round(1)
        out["dep_delay_avg_min"] = out["dep_delay_avg_min"].round(2)
        out["distance_mi"] = out["distance_mi"].round(1)
        out["cancel_rate"] = out["cancel_rate"].fillna(0.0)
        ctx.stage_end("aggregate", f"{len(out)} 条航线×天")

        result = engine.write("dws_route_daily", out, partition_value=period, mode="overwrite")
        ctx.rows_out = result["rows"]
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWS 航线日作业失败：%s", exc)
        return ctx.finish("failed", "E_DWS_ROUTE", str(exc))


# ---------------------------------------------------------------- 机场×30 分钟
def build_dws_delay_network(period: str, operator: str = "system") -> dict:
    year = period[:4]
    ctx = JobContext("dws_delay_network", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        df = _flags(_load_dwd(period))
        ctx.rows_in = len(df)
        slot = Config.CHINA_SLOT_MINUTES
        df["slot_seq"] = df["dep_slot"].fillna(0).astype(int)
        df["slot_id"] = (df["flight_date"].astype(str) + "#"
                         + df["slot_seq"].map(lambda v: f"{v:02d}"))
        df["slot_start_min"] = df["slot_seq"] * slot

        grouped = df.groupby(["origin", "slot_id"], as_index=False)
        out = grouped.agg(
            stat_date=("flight_date", "first"), slot_seq=("slot_seq", "first"),
            slot_start_min=("slot_start_min", "first"),
            flights=("flight_sk", "count"), _del15=("_del15", "sum"), _oper=("_oper", "sum"),
            dep_delay_avg_min=("dep_delay_min", "mean"),
            weather_class=("weather_class", lambda s: int(s.mode().iloc[0]) if len(s.mode()) else -1),
        )
        out = out.rename(columns={"origin": "airport_iata", "_del15": "dep_del15_cnt",
                                  "_oper": "_operated"})
        out["ontime_rate"] = ((out["_operated"] - out["dep_del15_cnt"])
                              / out["_operated"].replace(0, np.nan)).round(4)
        out["dep_delay_avg_min"] = out["dep_delay_avg_min"].round(2)
        out["risk_score"] = [
            risk_score(dr, ad, wc)
            for dr, ad, wc in zip(
                (out["dep_del15_cnt"] / out["_operated"].replace(0, np.nan)).fillna(0),
                out["dep_delay_avg_min"].fillna(0),
                out["weather_class"],
            )
        ]
        out["year"] = int(year)

        # 年分区是跨月累积的：先剔除本月的旧记录，再整体覆盖写回，保证幂等且不影响其他月份
        try:
            existing = engine.read("dws_delay_network", filters=[("year", "=", int(year))])
        except Exception:
            existing = pd.DataFrame()
        if not existing.empty:
            existing = existing[~existing["stat_date"].astype(str).str.startswith(period)]
            existing = existing.drop(columns=[c for c in ("year",) if c in existing.columns])
        combined = pd.concat([existing, out], ignore_index=True) if not existing.empty else out
        result = engine.write("dws_delay_network", combined, partition_value=year, mode="overwrite")
        ctx.rows_out = len(out)
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWS 网络时间片作业失败：%s", exc)
        return ctx.finish("failed", "E_DWS_NETWORK", str(exc))


# ---------------------------------------------------------------- 特征宽表
def _delay_rate_by(df: pd.DataFrame, keys: list[str], value_col: str = "_del15") -> pd.DataFrame:
    """按 key+日期算延误率，再 shift(1) + 7 日滚动 —— 只用"过去"的数据（S-06）。"""
    daily = df.groupby(keys + ["flight_date"], as_index=False).agg(
        n=("_oper", "sum"), d=(value_col, "sum"))
    daily["rate"] = daily["d"] / daily["n"].replace(0, np.nan)
    result = []
    for key, group in daily.groupby(keys):
        g = group.copy()
        g["flight_date"] = pd.to_datetime(g["flight_date"])
        g = g.set_index("flight_date").sort_index()
        g = g.reindex(pd.date_range(g.index.min(), g.index.max(), freq="D"))
        g[["n", "d"]] = g[["n", "d"]].fillna(0)
        g["rate"] = g["d"] / g["n"].replace(0, np.nan)
        prev = g["rate"].shift(1)
        # 7 个自然日、按航班数加权；不能把 7 个有航班的日期当成 7 天。
        roll = (g["d"].shift(1).rolling(7, min_periods=1).sum()
                / g["n"].shift(1).rolling(7, min_periods=1).sum().replace(0, np.nan))
        frame = pd.DataFrame({"flight_date": g.index.strftime("%Y-%m-%d")})
        for column, value in zip(keys, key if isinstance(key, tuple) else (key,)):
            frame[column] = value
        frame["rate_prev"] = prev.to_numpy()
        frame["rate_7d"] = roll.to_numpy()
        frame["traffic_prev"] = g["n"].shift(1).to_numpy()
        result.append(frame)
    return pd.concat(result, ignore_index=True) if result else pd.DataFrame()


def build_dws_flight_features(period: str, operator: str = "system") -> dict:
    ctx = JobContext("dws_flight_features", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        df = _flags(_load_dwd(period))
        ctx.rows_in = len(df)
        ctx.stage_start("network_features")

        history = df
        previous = prev_month(period)
        if previous in engine.BaseEngine.available_periods("dwd_flight_detail"):
            start = (pd.Timestamp(period + "-01") - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
            prior = engine.read("dwd_flight_detail", columns=DWD_COLUMNS,
                                filters=[("year_month", "=", previous), ("flight_date", ">=", start)])
            if not prior.empty:
                history = pd.concat([_flags(prior), df], ignore_index=True)
        origin_feat = _delay_rate_by(history, ["origin"])
        dest_feat = _delay_rate_by(history, ["dest"])
        route_feat = _delay_rate_by(history, ["route_key"])
        airline_feat = _delay_rate_by(history, ["reporting_airline"])

        df = df.merge(origin_feat.rename(columns={"rate_prev": "origin_delay_rate_prev_day",
                                                  "rate_7d": "origin_delay_rate_7d",
                                                  "traffic_prev": "origin_traffic_24h"})[
                          ["origin", "flight_date", "origin_delay_rate_prev_day",
                           "origin_delay_rate_7d", "origin_traffic_24h"]],
                      on=["origin", "flight_date"], how="left")
        df = df.merge(dest_feat.rename(columns={"rate_7d": "dest_delay_rate_7d"})[
                          ["dest", "flight_date", "dest_delay_rate_7d"]],
                      on=["dest", "flight_date"], how="left")
        df = df.merge(route_feat.rename(columns={"rate_7d": "route_delay_rate_7d"})[
                          ["route_key", "flight_date", "route_delay_rate_7d"]],
                      on=["route_key", "flight_date"], how="left")
        df = df.merge(airline_feat.rename(columns={"rate_7d": "airline_delay_rate_7d"})[
                          ["reporting_airline", "flight_date", "airline_delay_rate_7d"]],
                      on=["reporting_airline", "flight_date"], how="left")
        ctx.stage_end("network_features", "机场/航线/航司 7 日滞后延误率")

        ctx.stage_start("engineering")
        dt = pd.to_datetime(df["flight_date"], errors="coerce")
        df["month"] = dt.dt.month
        df["is_weekend"] = (df["day_of_week"] >= 5).astype(int)
        holidays = _holiday_map()
        df["is_holiday"] = df["flight_date"].map(holidays).fillna(0).astype(int)
        df["dow_sin"] = np.sin(2 * np.pi * df["day_of_week"] / 7).round(6)
        df["dow_cos"] = np.cos(2 * np.pi * df["day_of_week"] / 7).round(6)
        df["hour_sin"] = np.sin(2 * np.pi * df["dep_hour"] / 24).round(6)
        df["hour_cos"] = np.cos(2 * np.pi * df["dep_hour"] / 24).round(6)

        # 无历史的第 1 天：用中性先验 0.5（不是全月均值，避免未来信息泄漏）
        for col in ("origin_delay_rate_prev_day", "origin_delay_rate_7d",
                    "dest_delay_rate_7d", "route_delay_rate_7d", "airline_delay_rate_7d"):
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.5)
        df["origin_traffic_24h"] = pd.to_numeric(df["origin_traffic_24h"], errors="coerce")
        df["weather_class"] = pd.to_numeric(df["weather_class"], errors="coerce").fillna(-1).astype(int)
        df["weather_available"] = pd.to_numeric(df["weather_available"], errors="coerce").fillna(0).astype(int)
        df["dep_del15"] = pd.to_numeric(df["dep_del15"], errors="coerce")
        df["delay_level"] = pd.to_numeric(df["delay_level"], errors="coerce").fillna(-1).astype(int)
        df["chain_available"] = df["chain_available"].fillna(0).astype(int)
        df["risk_flag"] = df["risk_flag"].fillna(0).astype(int)
        ctx.stage_end("engineering")

        keep = ["flight_sk", "year_month", "flight_date", "origin", "dest", "route_key",
                "reporting_airline"] + [c for c in MODEL_FEATURES if c in df.columns] + \
               ["dep_delay_min", "dep_del15", "delay_level"]
        # 白名单校验：不允许出现任何黑名单字段（A-8 泄漏检查）
        leaked = [c for c in keep if c in Config.FEATURE_BLACKLIST and c not in
                  ("dep_delay_min", "dep_del15", "delay_level")]
        if leaked:
            raise ValueError(f"特征宽表出现禁用字段：{leaked}")
        out = df[[c for c in keep if c in df.columns]]

        result = engine.write("dws_flight_features", out, partition_value=period, mode="overwrite")
        ctx.rows_out = result["rows"]
        ctx.info("特征列 %d 个，标签列 3 个", len(MODEL_FEATURES))
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWS 特征宽表作业失败：%s", exc)
        return ctx.finish("failed", "E_DWS_FEATURES", str(exc))


def _holiday_map() -> dict[str, int]:
    try:
        dim = engine.read("dim_date", columns=["date_key", "is_holiday"])
        return dict(zip(dim["date_key"].astype(str), dim["is_holiday"].fillna(False).astype(int)))
    except Exception:
        return {}


def run(periods: list[str], operator: str = "system") -> dict:
    results = {}
    for period in periods:
        for name, fn in (("dws_airport_hourly", build_dws_airport_hourly),
                         ("dws_route_daily", build_dws_route_daily),
                         ("dws_delay_network", build_dws_delay_network),
                         ("dws_flight_features", build_dws_flight_features)):
            try:
                results[f"{name}:{period}"] = fn(period, operator=operator)
            except Exception as exc:
                results[f"{name}:{period}"] = {"status": "failed", "err_msg": str(exc)}
    return results

