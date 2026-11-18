"""ODS 入湖作业：BTS / NOAA ISD / US Weather Events（通道 A 数据面）。"""
from __future__ import annotations

import csv
import time
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

from app.config import Config
from app.etl.common import (
    JobContext, record_audit, record_failure, to_num, upsert_manifest,
)
from app.services import datasource, engine
from app.utils.timeutil import ym_of

# BTS 归一化列名 → ODS 列名（R-01 列名归一后的大小写无关映射）
BTS_FIELD_MAP = {
    "flightdate": "flight_date",
    "reporting_airline": "reporting_airline",
    "tail_number": "tail_number",
    "flight_number_reporting_airline": "flight_number",
    "origin": "origin",
    "origincityname": "origin_city_name",
    "dest": "dest",
    "destcityname": "dest_city_name",
    "crsdeptime": "crs_dep_time",
    "deptime": "dep_time",
    "depdelay": "dep_delay",
    "depdelayminutes": "dep_delay_minutes",
    "depdel15": "dep_del15",
    "crsarrtime": "crs_arr_time",
    "arrtime": "arr_time",
    "arrdelay": "arr_delay",
    "arrdelayminutes": "arr_delay_minutes",
    "arrdel15": "arr_del15",
    "taxiout": "taxi_out",
    "taxiin": "taxi_in",
    "airtime": "air_time",
    "crselapsedtime": "crs_elapsed_time",
    "actualelapsedtime": "actual_elapsed_time",
    "cancelled": "cancelled",
    "cancellationcode": "cancellation_code",
    "diverted": "diverted",
    "distance": "distance",
    "distancegroup": "distance_group",
    "carrierdelay": "carrier_delay",
    "weatherdelay": "weather_delay",
    "nasdelay": "nas_delay",
    "securitydelay": "security_delay",
    "lateaircraftdelay": "late_aircraft_delay",
}

ISD_COLUMNS = ("STATION", "DATE", "SOURCE", "LATITUDE", "LONGITUDE", "ELEVATION",
               "NAME", "REPORT_TYPE", "WND", "CIG", "VIS", "TMP", "DEW", "SLP", "AA1", "REM")

WEATHER_EVENT_FIELD_MAP = {
    "eventid": "event_id", "event_id": "event_id",
    "type": "event_type", "eventtype": "event_type", "event_type": "event_type",
    "severity": "severity",
    "starttime_utc": "start_time_utc", "starttimeutc": "start_time_utc",
    "endtime_utc": "end_time_utc", "endtimeutc": "end_time_utc",
    "precip_in": "precipitation", "precipitation": "precipitation",
    "airportcode": "airport_code", "airport_code": "airport_code",
    "locationlat": "lat", "locationlat_": "lat", "lat": "lat",
    "locationlng": "lon", "lng": "lon", "lon": "lon",
    "city": "city", "county": "county", "state": "state", "zipcode": "zipcode",
}


# ---------------------------------------------------------------- BTS
def _bts_zip_for_period(period: str) -> Path | None:
    for item in datasource.scan_files("bts"):
        if item["period"] == period:
            return Path(Config.DATA_ROOT) / item["rel_path"]
    return None


def read_bts_month(zip_path: Path, limit: int | None = None) -> tuple[pd.DataFrame, int]:
    """读取一个月度压缩包 → 归一化 DataFrame（全字符串保真）。"""
    entry = datasource.bts_csv_entry(zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(entry) as fh:
            header_raw = fh.readline().decode("utf-8", errors="replace")
    original = next(csv.reader([header_raw]))
    normalized = [datasource.normalize_header(h) for h in original]
    wanted = [(orig, BTS_FIELD_MAP[norm]) for orig, norm in zip(original, normalized)
              if norm in BTS_FIELD_MAP]
    if not wanted:
        raise ValueError(f"{zip_path.name} 表头不符合 BTS 格式，样例：{original[:8]}")
    usecols = [orig for orig, _ in wanted]
    rename = {orig: target for orig, target in wanted}

    with zipfile.ZipFile(zip_path) as zf:
        with zf.open(entry) as fh:
            reader = pd.read_csv(
                fh, usecols=usecols, dtype=str, keep_default_na=False, na_values=[""],
                nrows=limit, low_memory=False,
            )
    reader = reader.rename(columns=rename)
    # 统一字符串口径（BTS 存在 "0900" 与 900 混写、"-5.00" 等形态，ODS 不做类型转换）
    for col in reader.columns:
        reader[col] = reader[col].astype("string").str.strip()
    raw_rows = len(reader)
    reader["src_line"] = np.arange(2, raw_rows + 2, dtype="int64")
    return reader, raw_rows


def ingest_bts(periods: list[str], mode: str = "incremental", limit: int | None = None,
               operator: str = "system") -> dict:
    ctx = JobContext("ingest_bts", dataset="bts", period=",".join(periods), mode=mode,
                     operator=operator, engine=engine.get_engine().name)
    ctx.total_files = len(periods)
    ctx.persist(status="running")
    try:
        for idx, period in enumerate(periods, start=1):
            ctx.set_progress(idx - 1, len(periods), f"bts_{period}")
            zip_path = _bts_zip_for_period(period)
            if zip_path is None:
                ctx.drops.add("E01", 1)
                record_failure(ctx, f"bts_{period}.zip", period, "E_MISSING_SOURCE", "源文件不存在")
                continue
            ctx.stage_start(f"read_{period}")
            t0 = time.time()
            try:
                df, raw_rows = read_bts_month(zip_path, limit=limit)
            except Exception as exc:
                ctx.error("读取 %s 失败：%s", zip_path.name, exc)
                record_failure(ctx, zip_path.name, period, "E_ZIP_CORRUPT", str(exc))
                continue
            ctx.rows_in += raw_rows
            ctx.stage_end(f"read_{period}", f"{raw_rows} 行")

            df["src_file"] = zip_path.name
            ctx.stage_start(f"write_{period}")
            result = engine.write("ods_bts_flight", df, partition_value=period, mode="overwrite")
            ctx.rows_out += result["rows"]
            ctx.stage_end(f"write_{period}", f"{result['rows']} 行 / {result['files']} 文件")

            md5 = datasource.file_md5(zip_path)
            record_audit(ctx, zip_path.name, period, md5, zip_path.stat().st_size,
                         raw_rows, result["rows"], "success", time.time() - t0)
            upsert_manifest("bts", period, zip_path.name, zip_path.relative_to(Config.DATA_ROOT).as_posix(), md5,
                            zip_path.stat().st_size, result["rows"])
            ctx.set_progress(idx, len(periods), f"bts_{period}")
            ctx.persist(status="running")
            ctx.info("月份 %s 入湖完成：%d 行", period, result["rows"])
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("BTS 入湖失败：%s", exc)
        return ctx.finish("failed", "E_INGEST_FAILED", str(exc))


# ---------------------------------------------------------------- NOAA ISD
def _isd_files_for_year(year: str) -> list[Path]:
    return [Path(Config.DATA_ROOT) / f["rel_path"] for f in datasource.scan_files("isd")
            if f["period"] == year]


def read_isd_station(path: Path) -> pd.DataFrame:
    """单个站点年度 CSV → ODS 结构。"""
    df = pd.read_csv(path, usecols=lambda c: c in ISD_COLUMNS, dtype=str,
                     keep_default_na=False, low_memory=False)
    df.columns = [datasource.normalize_header(c) for c in df.columns]
    rename = {
        "station": "station_id", "date": "obs_time_utc", "source": "source",
        "latitude": "latitude", "longitude": "longitude", "elevation": "elevation_m",
        "name": "station_name", "report_type": "report_type",
        "wnd": "wnd", "cig": "cig", "vis": "vis", "tmp": "tmp", "dew": "dew",
        "slp": "slp", "aa1": "aa1", "rem": "remarks",
    }
    df = df.rename(columns=rename)
    df["src_file"] = path.name
    return df


def ingest_isd(years: list[str], mode: str = "incremental", max_stations: int | None = None,
               operator: str = "system", batch_files: int = 25) -> dict:
    ctx = JobContext("ingest_isd", dataset="isd", period=",".join(years), mode=mode,
                     operator=operator, engine=engine.get_engine().name)
    try:
        for year in years:
            files = _isd_files_for_year(year)
            if max_stations:
                files = files[:max_stations]
            ctx.total_files += len(files)
            if not files:
                ctx.warn("年份 %s 无站点文件", year)
                continue
            ctx.info("年份 %s：%d 个站点文件", year, len(files))
            first_batch = True
            for start in range(0, len(files), batch_files):
                chunk = files[start:start + batch_files]
                ctx.set_progress(start, len(files), chunk[0].name)
                frames = []
                for path in chunk:
                    try:
                        frames.append(read_isd_station(path))
                    except Exception as exc:
                        ctx.error("读取 %s 失败：%s", path.name, exc)
                        record_failure(ctx, path.name, year, "E_PARSE", str(exc))
                if not frames:
                    continue
                df = pd.concat(frames, ignore_index=True)
                ctx.rows_in += len(df)
                result = engine.write("ods_isd_weather", df, partition_value=year,
                                      mode="overwrite" if first_batch else "append",
                                      target_files=8)
                ctx.rows_out += result["rows"]
                first_batch = False
                ctx.info("年份 %s 批次 %d-%d 写入 %d 行", year, start + 1,
                         start + len(chunk), result["rows"])
                record_audit(ctx, f"isd_{year}_batch{start // batch_files + 1}", year, "", 0,
                             len(df), result["rows"], "success", 0.0)
            ctx.set_progress(len(files), len(files), f"isd_{year}")
            ctx.persist(status="running")
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("ISD 入湖失败：%s", exc)
        return ctx.finish("failed", "E_INGEST_FAILED", str(exc))


# ---------------------------------------------------------------- US Weather Events
def _weather_events_zip() -> Path | None:
    files = datasource.scan_files("weatherevents")
    return Path(Config.DATA_ROOT) / files[0]["rel_path"] if files else None


def ingest_weather_events(mode: str = "incremental", max_rows: int | None = None,
                          chunk_size: int = 200_000, operator: str = "system") -> dict:
    ctx = JobContext("ingest_weatherevents", dataset="weatherevents", period="all", mode=mode,
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    zip_path = _weather_events_zip()
    if zip_path is None:
        return ctx.finish("failed", "E_MISSING_SOURCE", "未找到 06_US天气事件 压缩包")
    try:
        # 这个数据集是 1.04 GB 单 CSV，但我们只做"逐块流式读取"（不落盘），
        # 因此把单文件上限放宽到单包上限，仍然守住压缩炸弹的总量闸门。
        entries = datasource.zip_entries(zip_path,
                                         max_entry_bytes=Config.ZIP_MAX_INFLATE_BYTES)
        entry = next((n for n, _ in entries if n.lower().endswith(".csv")), None)
        if entry is None:
            return ctx.finish("failed", "E_ZIP_CORRUPT", "压缩包内没有 CSV")
        ctx.total_files = 1
        written_years: set[str] = set()
        total = 0
        with zipfile.ZipFile(zip_path) as zf:
            with zf.open(entry) as fh:
                header = pd.read_csv(fh, nrows=0)
                fh.seek(0)
                mapping = {}
                for col in header.columns:
                    key = datasource.normalize_header(col)
                    if key in WEATHER_EVENT_FIELD_MAP:
                        mapping[col] = WEATHER_EVENT_FIELD_MAP[key]
                if not mapping:
                    return ctx.finish("failed", "E_SCHEMA_MISMATCH",
                                      f"字段无法映射，表头：{list(header.columns)[:10]}")
                reader = pd.read_csv(fh, usecols=list(mapping), dtype=str,
                                     chunksize=chunk_size, low_memory=False)
                for i, chunk in enumerate(reader):
                    chunk = chunk.rename(columns=mapping)
                    start = chunk.get("start_time_utc")
                    if start is None:
                        break
                    years = pd.to_datetime(start, errors="coerce", utc=True).dt.year
                    chunk = chunk.assign(year=years.astype("Int64"))
                    for year, group in chunk.groupby("year"):
                        if pd.isna(year):
                            ctx.drops.add("E01", len(group))
                            continue
                        year_str = str(int(year))
                        engine.write("ods_weather_event", group.drop(columns=["year"]),
                                     partition_value=year_str,
                                     mode="overwrite" if year_str not in written_years else "append")
                        written_years.add(year_str)
                    total += len(chunk)
                    ctx.rows_in = total
                    ctx.rows_out = total
                    if max_rows and total >= max_rows:
                        ctx.info("达到行数上限 %d，提前结束", max_rows)
                        break
                    if i % 10 == 0:
                        ctx.persist(status="running")
        ctx.set_progress(1, 1, entry)
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("天气事件入湖失败：%s", exc)
        return ctx.finish("failed", "E_INGEST_FAILED", str(exc))

