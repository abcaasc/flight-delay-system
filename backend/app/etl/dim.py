"""DIM 维表作业：机场 / 航司 / 日期 / 节假日（小表全量替换，供广播）。"""
from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import Config
from app.etl.common import JobContext, record_audit
from app.services import datasource, engine
from app.utils.timeutil import iter_months


def _dataset_files(key: str) -> list[Path]:
    return [Path(Config.DATA_ROOT) / f["rel_path"] for f in datasource.scan_files(key)]


def _find(key: str, name: str) -> Path | None:
    for path in _dataset_files(key):
        if path.name.lower() == name.lower():
            return path
    return None


# ---------------------------------------------------------------- 机场
def build_dim_airport(operator: str = "system") -> dict:
    ctx = JobContext("dim_airport", dataset="ourairports", mode="full", operator=operator,
                     engine=engine.get_engine().name)
    ctx.persist(status="running")
    airports_path = _find("ourairports", "airports.csv")
    if airports_path is None:
        return ctx.finish("failed", "E_MISSING_SOURCE", "未找到 airports.csv")

    ctx.stage_start("read_airports")
    raw = pd.read_csv(airports_path, dtype=str, keep_default_na=False, low_memory=False)
    ctx.rows_in = len(raw)
    ctx.stage_end("read_airports", f"{len(raw)} 行")

    # 只保留有 IATA 三字码的机场（BTS 用 IATA 关联）
    df = raw.copy()
    df["airport_iata"] = df["iata_code"].str.strip().str.upper()
    df["icao_code_raw"] = df["icao_code"].str.strip().str.upper()
    df = df[df["airport_iata"].str.fullmatch(r"[A-Z]{3}", na=False)].copy()
    ctx.info("有 IATA 码的机场：%d / %d", len(df), len(raw))

    # 跑道容量（runways.csv）
    runways_path = _find("ourairports", "runways.csv")
    runway_stats = pd.DataFrame(columns=["airport_ident", "runway_count", "max_runway_ft"])
    if runways_path is not None:
        ctx.stage_start("read_runways")
        rw = pd.read_csv(runways_path, dtype=str, keep_default_na=False, low_memory=False)
        rw["length_ft"] = pd.to_numeric(rw["length_ft"], errors="coerce")
        runway_stats = (rw.groupby("airport_ident")
                        .agg(runway_count=("id", "count"), max_runway_ft=("length_ft", "max"))
                        .reset_index())
        ctx.stage_end("read_runways", f"{len(rw)} 条跑道")

    # 时区（OurAirports 无 tz，取 OpenFlights airports.dat 的 tz 列，§3.5 展示口径需要）
    tz_map = _openflights_timezone()

    out = pd.DataFrame({
        "airport_iata": df["airport_iata"],
        "icao_code": df["icao_code_raw"].where(df["icao_code_raw"].str.len() == 4, df["ident"].str.upper()),
        "ident": df["ident"].str.upper(),
        "name": df["name"],
        "city": df["municipality"],
        "country": df["iso_country"].str.upper(),
        "iso_region": df["iso_region"],
        "lat": pd.to_numeric(df["latitude_deg"], errors="coerce"),
        "lon": pd.to_numeric(df["longitude_deg"], errors="coerce"),
        "elevation_ft": pd.to_numeric(df["elevation_ft"], errors="coerce"),
        "airport_type": df["type"],
        "scheduled_service": df["scheduled_service"],
    })
    out["tz"] = out["airport_iata"].map(tz_map)
    out = out.merge(runway_stats.rename(columns={"airport_ident": "ident"}),
                    on="ident", how="left")
    out["runway_count"] = out["runway_count"].fillna(0).astype("int64")
    out["is_us"] = out["country"].eq("US")
    out["is_china"] = out["country"].eq("CN")
    out["in_bts"] = out["is_us"] & out["scheduled_service"].eq("yes")

    ctx.stage_start("write")
    result = engine.write("dim_airport", out, mode="overwrite", target_files=1)
    ctx.rows_out = result["rows"]
    ctx.stage_end("write", f"{result['rows']} 行")
    record_audit(ctx, airports_path.name, "", datasource.file_md5(airports_path),
                 airports_path.stat().st_size, ctx.rows_in, result["rows"], "success", 0.0,
                 channel="C")
    return ctx.finish("success")


def _openflights_timezone() -> dict[str, str]:
    """OpenFlights airports.dat → {IATA: tz 名称}。"""
    path = _find("openflights", "airports.dat")
    out: dict[str, str] = {}
    if path is None:
        return out
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.reader(fh):
            if len(row) < 12:
                continue
            iata = (row[4] or "").strip().upper()
            tz = (row[11] or "").strip()
            if len(iata) == 3 and tz:
                out[iata] = tz
    return out


# ---------------------------------------------------------------- 航司
AIRLINE_FIELDS = ("airline_id", "name", "alias", "iata", "icao", "callsign", "country", "active")


def build_dim_airline(operator: str = "system") -> dict:
    ctx = JobContext("dim_airline", dataset="openflights", mode="full", operator=operator,
                     engine=engine.get_engine().name)
    ctx.persist(status="running")
    path = _find("openflights", "airlines.dat")
    if path is None:
        return ctx.finish("failed", "E_MISSING_SOURCE", "未找到 airlines.dat")
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for rec in csv.reader(fh):
            if len(rec) < 8:
                continue
            code = (rec[3] or "").strip().upper()
            if not (len(code) == 2 and code.isalpha()):
                continue
            rows.append({
                "airline_code": code,
                "name": rec[1].strip(),
                "icao": (rec[4] or "").strip().upper(),
                "callsign": (rec[5] or "").strip(),
                "country": (rec[6] or "").strip(),
                "active": (rec[7] or "").strip().upper() == "Y",
            })
    df = pd.DataFrame(rows).drop_duplicates(subset=["airline_code"], keep="first")
    ctx.rows_in = len(df)
    result = engine.write("dim_airline", df, mode="overwrite", target_files=1)
    ctx.rows_out = result["rows"]
    record_audit(ctx, path.name, "", datasource.file_md5(path), path.stat().st_size,
                 ctx.rows_in, result["rows"], "success", 0.0, channel="C")
    return ctx.finish("success")


# ---------------------------------------------------------------- 日期与节假日
def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """某月第 n 个星期 weekday（weekday: 周一=0）。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def us_federal_holidays(year: int) -> list[tuple[date, str, str]]:
    """美国联邦假日 11 项（含 observed 规则简化为按名义日期）。"""
    return [
        (date(year, 1, 1), "New Year's Day", "federal"),
        (_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day", "federal"),
        (_nth_weekday(year, 2, 0, 3), "Presidents' Day", "federal"),
        (_last_weekday(year, 5, 0), "Memorial Day", "federal"),
        (date(year, 6, 19), "Juneteenth", "federal"),
        (date(year, 7, 4), "Independence Day", "federal"),
        (_nth_weekday(year, 9, 0, 1), "Labor Day", "federal"),
        (_nth_weekday(year, 10, 0, 2), "Columbus Day", "federal"),
        (date(year, 11, 11), "Veterans Day", "federal"),
        (_nth_weekday(year, 11, 3, 4), "Thanksgiving Day", "federal"),
        (date(year, 12, 25), "Christmas Day", "federal"),
    ]


def build_dim_date(start: str = "2015-01-01", end: str = "2024-12-31",
                   operator: str = "system") -> dict:
    ctx = JobContext("dim_date", dataset="calendar", mode="full", operator=operator,
                     engine=engine.get_engine().name)
    ctx.persist(status="running")
    days = pd.date_range(start, end, freq="D")
    ctx.rows_in = len(days)

    holiday_rows = []
    for year in range(int(start[:4]), int(end[:4]) + 1):
        for day, name, kind in us_federal_holidays(year):
            holiday_rows.append({"date_key": day.strftime("%Y-%m-%d"), "region": "US",
                                 "holiday_name": name, "holiday_type": kind,
                                 "is_observed": True})
    holiday_df = pd.DataFrame(holiday_rows).drop_duplicates(subset=["date_key", "region"])
    holiday_map = (holiday_df[holiday_df.region == "US"]
                   .set_index("date_key")["holiday_name"].to_dict())

    df = pd.DataFrame({
        "date_key": days.strftime("%Y-%m-%d"),
        "year": days.year.astype("int64"),
        "month": days.month.astype("int64"),
        "day": days.day.astype("int64"),
        "quarter": days.quarter.astype("int64"),
        "weekday": days.weekday.astype("int64"),
        "week_of_year": days.isocalendar().week.to_numpy().astype("int64"),
    })
    df["is_weekend"] = df["weekday"] >= 5
    df["holiday_name"] = df["date_key"].map(holiday_map).fillna("")
    df["is_holiday"] = df["holiday_name"].ne("")
    df["year_month"] = df["date_key"].str.slice(0, 7)

    engine.write("dim_date", df, mode="overwrite", target_files=1)
    engine.write("dim_holiday", holiday_df, mode="overwrite", target_files=1)
    ctx.rows_out = len(df)
    return ctx.finish("success")


def run_all(operator: str = "system") -> dict:
    results = {}
    for name, fn in (("dim_airport", build_dim_airport), ("dim_airline", build_dim_airline),
                     ("dim_date", build_dim_date)):
        try:
            results[name] = fn(operator=operator)
        except Exception as exc:
            results[name] = {"status": "failed", "err_msg": str(exc)}
    return results
