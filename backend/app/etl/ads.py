"""ADS 应用层作业：机场排行 / 异常预警（供 P3 排行与 P8 监控联动）。"""
from __future__ import annotations


import numpy as np
import pandas as pd

from app.config import Config
from app.etl.common import JobContext
from app.services import engine
from app.utils.logger import get_logger

log = get_logger(__name__)

RANK_METRICS = ("delay_rate", "ontime_rate", "cancel_rate", "avg_delay_min")


def build_ads_airport_rank(period: str, operator: str = "system", top_n: int = 60) -> dict:
    ctx = JobContext("ads_airport_rank", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        df = engine.read("dwd_flight_detail",
                         columns=["flight_sk", "flight_date", "origin", "dep_delay_min",
                                  "dep_del15", "cancelled", "delay_level"],
                         filters=[("year_month", "=", period)])
        if df.empty:
            return ctx.finish("failed", "E_NO_DATA", f"{period} 无 DWD 数据")
        ctx.rows_in = len(df)

        df["cancelled"] = pd.to_numeric(df["cancelled"], errors="coerce").fillna(0)
        df["dep_del15"] = pd.to_numeric(df["dep_del15"], errors="coerce")
        df["dep_delay_min"] = pd.to_numeric(df["dep_delay_min"], errors="coerce")
        df["_cancel"] = (df["cancelled"] == 1).astype(int)
        df["_oper"] = 1 - df["_cancel"]
        df["_del15"] = (df["dep_del15"].fillna(0) >= 1).astype(int)

        daily = df.groupby(["flight_date", "origin"], as_index=False).agg(
            flights=("flight_sk", "count"), _del15=("_del15", "sum"),
            _cancel=("_cancel", "sum"), _oper=("_oper", "sum"),
            avg_delay_min=("dep_delay_min", "mean"))
        daily["delay_rate"] = (daily["_del15"] / daily["_oper"].replace(0, np.nan)).round(4)
        daily["ontime_rate"] = ((daily["_oper"] - daily["_del15"]) / daily["_oper"].replace(0, np.nan)).round(4)
        daily["cancel_rate"] = (daily["_cancel"] / daily["flights"].replace(0, np.nan)).round(4)
        daily["avg_delay_min"] = daily["avg_delay_min"].round(2)

        frames = []
        for metric in RANK_METRICS:
            sub = daily[["flight_date", "origin", "flights", metric]].rename(
                columns={"flight_date": "stat_date", "origin": "airport_iata", metric: "value"})
            sub = sub.dropna(subset=["value"])
            ascending = metric in ("avg_delay_min", "cancel_rate", "delay_rate")
            for stat_date, group in sub.groupby("stat_date"):
                ranked = group.sort_values("value", ascending=ascending).head(top_n).copy()
                ranked["rank_no"] = range(1, len(ranked) + 1)
                ranked["metric"] = metric
                frames.append(ranked)

        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if out.empty:
            return ctx.finish("failed", "E_EMPTY", "排行结果为空")
        out["year_month"] = period

        # 环比：同一机场同一指标，与上一统计日相比
        out = out.sort_values(["metric", "airport_iata", "stat_date"])
        out["prev_value"] = out.groupby(["metric", "airport_iata"])["value"].shift(1)
        out["delta_pct"] = ((out["value"] - out["prev_value"])
                            / out["prev_value"].abs().replace(0, np.nan) * 100).round(2)
        out = out.drop(columns=["prev_value"])

        result = engine.write("ads_airport_rank", out, partition_value=period, mode="overwrite",
                              target_files=2)
        ctx.rows_out = result["rows"]
        ctx.info("排行 %d 行（%d 个指标 × %d 天）", len(out), len(RANK_METRICS),
                 out["stat_date"].nunique())
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("ADS 排行作业失败：%s", exc)
        return ctx.finish("failed", "E_ADS_RANK", str(exc))


def build_ads_anomaly_alert(period: str, operator: str = "system",
                            sigma: float = 2.0, cancel_threshold: float = 0.10) -> dict:
    ctx = JobContext("ads_anomaly_alert", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        dws = engine.read("dws_airport_hourly", filters=[("year_month", "=", period)])
        if dws.empty:
            return ctx.finish("failed", "E_NO_DATA", f"{period} 无 dws_airport_hourly")
        ctx.rows_in = len(dws)

        daily = dws.groupby(["stat_date", "airport_iata"], as_index=False).agg(
            flights=("flights", "sum"), dep_del15_cnt=("dep_del15_cnt", "sum"),
            cancelled_cnt=("cancelled_cnt", "sum"))
        daily["delay_rate"] = (daily["dep_del15_cnt"] / daily["flights"].replace(0, np.nan)).round(4)
        daily["cancel_rate"] = (daily["cancelled_cnt"] / daily["flights"].replace(0, np.nan)).round(4)

        stats = daily.groupby("airport_iata")["delay_rate"].agg(["mean", "std"]).reset_index()
        daily = daily.merge(stats, on="airport_iata", how="left")
        daily["upper"] = daily["mean"] + sigma * daily["std"].fillna(0)

        alerts = []
        for _, row in daily.iterrows():
            date_key = str(row["stat_date"]).replace("-", "")
            if pd.notna(row["delay_rate"]) and pd.notna(row["upper"]) and row["delay_rate"] > row["upper"]:
                alerts.append({
                    "alert_id": f"AL{date_key}{row['airport_iata']}D",
                    "alert_date": row["stat_date"], "year_month": period,
                    "airport_iata": row["airport_iata"], "rule": "delay_rate_sigma",
                    "observed": round(float(row["delay_rate"]), 4),
                    "threshold": round(float(row["upper"]), 4), "level": "critical",
                    "message": (f"{row['airport_iata']} 延误率 {row['delay_rate']:.2%} "
                                f"超过该机场均值+{sigma:g}σ（{row['upper']:.2%}）"),
                })
            if pd.notna(row["cancel_rate"]) and row["cancel_rate"] > cancel_threshold:
                alerts.append({
                    "alert_id": f"AL{date_key}{row['airport_iata']}C",
                    "alert_date": row["stat_date"], "year_month": period,
                    "airport_iata": row["airport_iata"], "rule": "cancel_rate_abs",
                    "observed": round(float(row["cancel_rate"]), 4),
                    "threshold": cancel_threshold, "level": "warning",
                    "message": (f"{row['airport_iata']} 取消率 {row['cancel_rate']:.2%} "
                                f"超过阈值 {cancel_threshold:.0%}"),
                })
        if not alerts:
            ctx.info("本月无异常预警")
            return ctx.finish("success")
        out = pd.DataFrame(alerts)
        result = engine.write("ads_anomaly_alert", out, partition_value=period, mode="overwrite",
                              target_files=1)
        ctx.rows_out = result["rows"]
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("ADS 预警作业失败：%s", exc)
        return ctx.finish("failed", "E_ADS_ALERT", str(exc))



def run(periods: list[str], operator: str = "system") -> dict:
    results = {}
    for period in periods:
        results[f"ads_airport_rank:{period}"] = build_ads_airport_rank(period, operator)
        results[f"ads_anomaly_alert:{period}"] = build_ads_anomaly_alert(period, operator)
    return results

