"""DWD 明细层作业：气象解析 + 航班清洗 / 航班链重建 / 气象时间窗对齐。

清洗规则（设计说明书 §3.4 R-01~R-07）与本文件函数的对应关系：
    R-01 列名归一       → datasource.normalize_header（入湖阶段已完成）
    R-02 HHMM 转换      → common.to_clock_minutes
    R-03 负延误保留     → 不做 clip，只打 outlier
    R-04 取消航班单列   → cancelled=1，delay_level=-1，不进分类训练集
    R-05 原因字段禁用   → 写入 DWD 但不进特征白名单（catalog.MODEL_FEATURES）
    R-06 重复行剔除     → 唯一键 (flight_date, airline, flight_number, origin)
    R-07 关键时间缺失   → 剔除并计数（E01）
"""
from __future__ import annotations

import hashlib
import time

import numpy as np
import pandas as pd

from app.config import Config
from app.etl import isd_codec
from app.etl.common import JobContext, record_audit, to_clock_minutes, to_num
from app.services import engine, weather_map
from app.utils.logger import get_logger
from app.utils.response import ApiError
from app.utils.timeutil import prev_month, local_departure_utc

log = get_logger(__name__)


# ============================================================ 气象：ODS → DWD
def _parse_isd_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把 ISD 编码串解析成数值（向量化）。"""
    out = pd.DataFrame(index=df.index)

    wnd = df["wnd"].fillna("").str.split(",", expand=True)

    def _part(frame: pd.DataFrame, idx: int) -> pd.Series:
        """安全取第 idx 段：不足段数时返回全 NaN 序列（WND 有 4/6 段两种写法）。"""
        if idx in frame.columns:
            return frame[idx]
        return pd.Series(np.nan, index=frame.index, dtype="object")

    direction = pd.to_numeric(_part(wnd, 0), errors="coerce")
    speed_raw = pd.to_numeric(_part(wnd, 3), errors="coerce")
    gust_raw = pd.to_numeric(_part(wnd, 5), errors="coerce")
    # ISD 缺测标记 9999（=999.9 m/s）必须剔除，否则会被当成 300 m/s 的极端风
    speed = speed_raw.where(speed_raw < 9999) / 10.0
    gust = gust_raw.where(gust_raw < 9999) / 10.0
    out["wind_dir"] = direction.where(direction < 999)
    out["wind_ms"] = speed
    out["gust_ms"] = gust

    cig = pd.to_numeric(df["cig"].fillna("").str.split(",").str[0], errors="coerce")
    out["ceil_ft"] = (cig.where(cig < 99999) * 3.28084).round(1)
    vis = pd.to_numeric(df["vis"].fillna("").str.split(",").str[0], errors="coerce")
    out["vis_mi"] = (vis.where(vis < 999999) / 1609.344).round(3)

    tmp = pd.to_numeric(df["tmp"].fillna("").str.split(",").str[0].str.replace("+", "", regex=False),
                        errors="coerce")
    out["temp_c"] = (tmp.where(tmp.abs() < 9999) / 10.0).round(1)
    dew = pd.to_numeric(df["dew"].fillna("").str.split(",").str[0].str.replace("+", "", regex=False),
                        errors="coerce")
    out["dew_c"] = (dew.where(dew.abs() < 9999) / 10.0).round(1)
    slp = pd.to_numeric(df["slp"].fillna("").str.split(",").str[0], errors="coerce")
    out["slp_hpa"] = (slp.where(slp < 99999) / 10.0).round(1)

    # AA1 液态降水：深度可能在 parts[1] 或 parts[2]（见 isd_codec.parse_aa1 说明）
    parts = df["aa1"].fillna("").str.split(",")
    second = parts.str[1]
    third = parts.str[2]
    depth_text = second.where(second.str.len() > 1, third)
    depth = pd.to_numeric(depth_text.fillna("").str.replace("+", "", regex=False), errors="coerce")
    depth = depth.where(depth < 9999)  # 9999/99999 = 缺测
    out["precip_mm"] = (depth.fillna(0) / 10.0).round(1)

    out["weather_class"] = isd_codec.classify_vectorized(
        out["vis_mi"] * 1609.344, out["ceil_ft"] / 3.28084, out["wind_ms"],
        out["temp_c"], out["precip_mm"], df["remarks"],
    )
    return out


def _read_isd_source(year: str, needed_stations: set[str], ctx: JobContext) -> pd.DataFrame:
    """优先从 ODS 读；ODS 覆盖不足时回落到直接读站点文件。

    设计上 ISD 走 ODS（`flask ingest --dataset isd --periods 2015`），
    但 ODS 可能只入湖了部分站点；此时若直接用 ODS 会静默丢掉其余机场的气象，
    所以这里按"站点覆盖率 ≥90%"做闸门，覆盖率不足就回落并说明原因。
    """
    try:
        ods = engine.read("ods_isd_weather",
                          columns=["station_id", "obs_time_utc", "wnd", "cig", "vis", "tmp", "dew", "slp", "aa1", "remarks"],
                          filters=[("year", "=", year), ("station_id", "in", sorted(needed_stations))])
    except ApiError:
        ods = pd.DataFrame()
    if not ods.empty:
        covered = set(ods["station_id"].astype(str).unique()) & needed_stations
        coverage = len(covered) / max(1, len(needed_stations))
        if coverage >= 0.9:
            ctx.info("气象来源：ODS（站点覆盖 %.0f%%，%d 行）", coverage * 100, len(ods))
            ods = ods.rename(columns={"obs_time_utc": "date"})
            for col in ("wnd", "cig", "vis", "tmp", "dew", "slp", "aa1", "remarks"):
                if col not in ods.columns:
                    ods[col] = ""
            return ods[ods["station_id"].astype(str).isin(needed_stations)].copy()
        ctx.warn("ODS 站点覆盖率仅 %.0f%%（%d/%d），回落为直接读站点文件",
                 coverage * 100, len(covered), len(needed_stations))

    files = [p for p in weather_map.available_station_files(year) if p.stem in needed_stations]
    ctx.info("气象来源：站点文件（%d 个）", len(files))
    frames = []
    cols = ["STATION", "DATE", "WND", "CIG", "VIS", "TMP", "DEW", "SLP", "AA1", "REM"]
    for i, path in enumerate(files, start=1):
        try:
            part = pd.read_csv(path, usecols=lambda c: c in cols, dtype=str,
                               keep_default_na=False, low_memory=False)
        except Exception as exc:
            ctx.error("读取 %s 失败：%s", path.name, exc)
            continue
        part.columns = [c.strip().lower() for c in part.columns]
        part = part.rename(columns={"rem": "remarks"})   # 站点文件叫 REM，ODS 叫 remarks
        part["station_id"] = path.stem
        frames.append(part)
        if i % 20 == 0:
            ctx.set_progress(i, len(files), path.name)
            ctx.persist(status="running")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _aggregate_weather(raw: pd.DataFrame, station_map: dict, ctx: JobContext) -> pd.DataFrame:
    """按机场×小时聚合单个小批次；众数使用计数排序，避免逐小时 Python 回调。"""
    raw = raw.copy()
    for col in ("date", "wnd", "cig", "vis", "tmp", "dew", "slp", "aa1", "remarks"):
        if col not in raw:
            raw[col] = ""
    parsed = _parse_isd_frame(raw)
    raw = pd.concat([raw[["station_id", "date"]].reset_index(drop=True), parsed.reset_index(drop=True)], axis=1)
    raw["airport_iata"] = raw["station_id"].map(station_map)
    ctx.drops.flag("E08", int(raw["airport_iata"].isna().sum()))
    raw = raw[raw["airport_iata"].notna()].copy()
    raw["obs_ts"] = pd.to_datetime(raw["date"], errors="coerce", utc=True).dt.tz_localize(None)
    ctx.drops.add("E01", int(raw["obs_ts"].isna().sum()))
    raw = raw[raw["obs_ts"].notna()].copy()
    if raw.empty:
        return pd.DataFrame()
    raw["obs_hour"] = raw["obs_ts"].dt.floor("h")
    keys = ["airport_iata", "obs_hour"]
    hourly = raw.groupby(keys, as_index=False).agg(
        vis_mi=("vis_mi", "min"), ceil_ft=("ceil_ft", "min"), wind_ms=("wind_ms", "max"),
        gust_ms=("gust_ms", "max"), wind_dir=("wind_dir", "mean"), temp_c=("temp_c", "mean"),
        dew_c=("dew_c", "mean"), slp_hpa=("slp_hpa", "mean"), precip_mm=("precip_mm", "sum"))
    modes = raw.groupby(keys + ["weather_class"], as_index=False).size()
    modes = modes.sort_values(["size", "weather_class"], ascending=[False, True]).drop_duplicates(keys)
    hourly = hourly.merge(modes[keys + ["weather_class"]], on=keys, how="left")
    hourly["station_id"] = hourly["airport_iata"].map({v: k for k, v in station_map.items()})
    hourly["obs_hour"] = hourly["obs_hour"].dt.strftime("%Y-%m-%d %H:00")
    hourly["obs_time_utc"] = hourly["obs_hour"]
    hourly["weather_available"] = 1
    return hourly


def build_dwd_weather(year: str, operator: str = "system", airports: set[str] | None = None) -> dict:
    ctx = JobContext("dwd_weather_hourly", dataset="isd", period=year, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    try:
        station_map = weather_map.station_airport_map()
        files = weather_map.available_station_files(year)
        if airports is not None:
            files = [p for p in files if station_map.get(p.stem) in airports]
        if not files:
            return ctx.finish("failed", "E_MISSING_SOURCE", f"{year} 年无可用站点文件")
        # 同机场所有站点留在同一批次，保留跨站观测聚合语义。
        airport_ids = sorted({station_map[p.stem] for p in files})
        ctx.total_files = len(files)
        first = True
        for offset in range(0, len(airport_ids), 8):
            batch_airports = set(airport_ids[offset:offset + 8])
            stations = {p.stem for p in files if station_map[p.stem] in batch_airports}
            raw = _read_isd_source(year, stations, ctx)
            if raw.empty:
                raise ValueError(f"气象批次无可用记录：{sorted(batch_airports)}")
            ctx.rows_in += len(raw)
            hourly = _aggregate_weather(raw, station_map, ctx)
            if not hourly.empty:
                result = engine.write("dwd_weather_hourly", hourly, partition_value=year,
                                      mode="overwrite" if first else "append")
                ctx.rows_out += result["rows"]
                first = False
            ctx.done_files += len(stations)
            ctx.persist(status="running")
            ctx.info("气象批次完成：%d/%d 站，累计 %d 小时观测", ctx.done_files, len(files), ctx.rows_out)
        if first:
            return ctx.finish("failed", "E_PARSE", "所有气象记录均不可用")
        record_audit(ctx, f"isd_{year}", year, "", 0, ctx.rows_in, ctx.rows_out, "success", 0.0)
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWD 气象作业失败：%s", exc)
        return ctx.finish("failed", "E_DWD_WEATHER", str(exc))


# ============================================================ 航班：ODS → DWD
def _flight_sk(df: pd.DataFrame) -> pd.Series:
    key = (df["flight_date"].astype(str) + "|" + df["reporting_airline"].astype(str)
           + "|" + df["flight_number"].astype(str) + "|" + df["origin"].astype(str))
    return key.map(lambda s: hashlib.md5(s.encode("utf-8")).hexdigest()[:20])


def _window_weather_features(weather: pd.DataFrame) -> pd.DataFrame:
    """每个机场 → 逐小时窗口特征：取计划起飞前 1–3 小时（不含本小时，防泄漏）。"""
    keep = ["vis_mi", "wind_ms", "temp_c", "dew_c", "slp_hpa", "precip_mm"]
    frames = []
    weather = weather.copy()
    weather["obs_hour"] = pd.to_datetime(weather["obs_hour"], errors="coerce")
    weather = weather.dropna(subset=["obs_hour"])
    for airport, group in weather.groupby("airport_iata"):
        s = group.set_index("obs_hour").sort_index()
        grid = pd.date_range(s.index.min(), s.index.max() + pd.Timedelta(hours=3), freq="h")
        s = s.reindex(grid)
        rolled = s[keep].rolling(3, min_periods=1).agg(
            {c: ("min" if c == "vis_mi" else "max" if c == "wind_ms" else "sum" if c == "precip_mm" else "mean")
             for c in keep}
        ).shift(1)  # 窗口右端 = H-1，避免用到起飞时刻之后的数据（S-06 时序防泄漏）
        frame = pd.DataFrame({
            "origin": airport,
            "anchor_hour": grid,
            "vis_min_mi": rolled["vis_mi"].to_numpy(),
            "wind_max_ms": rolled["wind_ms"].to_numpy(),
            "tmp_mean_c": rolled["temp_c"].to_numpy(),
            "dew_mean_c": rolled["dew_c"].to_numpy(),
            "slp_mean_hpa": rolled["slp_hpa"].to_numpy(),
            "precip_mm": rolled["precip_mm"].to_numpy(),
            "weather_class": s["weather_class"].rolling(3, min_periods=1).max().shift(1).to_numpy(),
        })
        frames.append(frame)
    if not frames:
        return pd.DataFrame(columns=["origin", "anchor_hour", "vis_min_mi", "wind_max_ms",
                                     "tmp_mean_c", "dew_mean_c", "slp_mean_hpa",
                                     "precip_mm", "weather_class"])
    out = pd.concat(frames, ignore_index=True)
    out["weather_available"] = out[["vis_min_mi", "wind_max_ms", "tmp_mean_c", "precip_mm"]].notna().any(axis=1).astype(int)
    return out


def build_dwd_flight(period: str, operator: str = "system", limit: int | None = None) -> dict:
    ctx = JobContext("dwd_flight_detail", dataset="bts", period=period, mode="full",
                     operator=operator, engine=engine.get_engine().name)
    ctx.persist(status="running")
    ctx.total_files = 1
    try:
        ctx.stage_start("read_ods")
        ods = engine.read("ods_bts_flight", filters=[("year_month", "=", period)])
        if limit:
            ods = ods.head(limit)
        ctx.rows_in = len(ods)
        ctx.stage_end("read_ods", f"{len(ods)} 行")

        df = ods.copy()
        # ---------------- R-01/R-02：时间解析 ----------------
        ctx.stage_start("parse_time")
        df["flight_date_parsed"] = pd.to_datetime(df["flight_date"], errors="coerce")
        crs_dep_min, crs_status = to_clock_minutes(df["crs_dep_time"])
        dep_min, dep_status = to_clock_minutes(df["dep_time"])
        crs_arr_min, _ = to_clock_minutes(df["crs_arr_time"])
        df["crs_dep_min"] = crs_dep_min
        df["dep_min"] = dep_min
        # E02：非空但不是合法 HHMM（排除 2400 折算）
        invalid_clock = ((crs_status.isin(["invalid"])) | (dep_status.isin(["invalid"]))).sum()
        ctx.drops.add("E02", int(invalid_clock))
        fixed_2400 = int((crs_status == "fixed_2400").sum() + (dep_status == "fixed_2400").sum())
        ctx.stage_end("parse_time", f"2400 折算 {fixed_2400} 处")

        df["cancelled"] = to_num(df["cancelled"]).fillna(0).astype(int)
        df["diverted"] = to_num(df["diverted"]).fillna(0).astype(int)

        # ---------------- R-07：关键时间缺失 ----------------
        mask_missing = df["flight_date_parsed"].isna() | df["crs_dep_min"].isna()
        mask_missing |= (df["cancelled"] != 1) & df["dep_min"].isna()
        n_missing = int(mask_missing.sum())
        ctx.drops.add("E01", n_missing)

        # ---------------- E04：日期越界 / E03：机场代码非法 ----------------
        lo, hi = Config.BTS_DATE_RANGE
        date_ok = df["flight_date_parsed"].between(pd.Timestamp(lo), pd.Timestamp(hi))
        origin_ok = df["origin"].astype("string").str.fullmatch(r"[A-Z]{3}", na=False)
        dest_ok = df["dest"].astype("string").str.fullmatch(r"[A-Z]{3}", na=False)
        ctx.drops.add("E04", int((~date_ok & ~mask_missing).sum()))
        ctx.drops.add("E03", int((~(origin_ok & dest_ok) & ~mask_missing).sum()))
        keep = ~mask_missing & date_ok & origin_ok & dest_ok
        df = df[keep].copy()
        if df.empty:
            return ctx.finish("failed", "E_ALL_DROPPED", "清洗后无有效记录")

        # ---------------- R-03/R-04/R-05：延误口径与标签 ----------------
        df["dep_delay_min"] = to_num(df["dep_delay"]).round(2)
        df["arr_delay_min"] = to_num(df["arr_delay"]).round(2)
        df["crs_elapsed_min"] = to_num(df["crs_elapsed_time"])
        df["actual_elapsed_min"] = to_num(df["actual_elapsed_time"])
        df["air_time_min"] = to_num(df["air_time"])
        df["taxi_out_min"] = to_num(df["taxi_out"])
        df["taxi_in_min"] = to_num(df["taxi_in"])
        df["distance_mi"] = to_num(df["distance"])

        # 官方 DepDel15 优先，缺失时按口径自算
        official_del15 = to_num(df["dep_del15"])
        computed_del15 = (df["dep_delay_min"] >= Config.ONTIME_THRESHOLD_MIN).astype("float64").where(df["dep_delay_min"].notna())
        df["dep_del15"] = official_del15.where(official_del15.notna(), computed_del15)
        df.loc[df["cancelled"] == 1, "dep_del15"] = np.nan
        df["arr_del15"] = to_num(df["arr_del15"])

        # S-02 延误双口径交叉校验：自算 = 实际起飞 - 计划起飞（跨日 +1440）
        check = df["dep_min"] - df["crs_dep_min"]
        check = check.where(check > -720, check + 1440)
        df["dep_delay_check"] = check
        df["delay_check_diff"] = (df["dep_delay_min"] - df["dep_delay_check"]).round(2)
        mismatch = int((df["delay_check_diff"].abs() > 30).sum())
        if mismatch:
            ctx.info("双口径差异 >30min 的记录：%d 条（已留痕，不剔除）", mismatch)

        # E07：值域检查（保留但打标，不删除）
        out_of_range = (~df["dep_delay_min"].between(Config.DELAY_MIN_BOUND, Config.DELAY_MAX_BOUND))
        df["outlier"] = (out_of_range & df["dep_delay_min"].notna()).astype(int)
        ctx.drops.flag("E07", int(df["outlier"].sum()))

        # 三级延误分类（ICAO 口径）：0 正常 <15 / 1 轻微 15-60 / 2 严重 >60
        level = pd.Series(0, index=df.index, dtype="int64")
        level = level.mask(df["dep_delay_min"] >= Config.ONTIME_THRESHOLD_MIN, 1)
        level = level.mask(df["dep_delay_min"] > Config.SEVERE_DELAY_MIN, 2)
        level = level.where((df["cancelled"] != 1) & df["dep_delay_min"].notna(), -1)
        df["delay_level"] = level

        # R-04：取消航班单独处理（不剔除，靠 delay_level=-1 与 dep_del15=NaN 隔离）
        n_cancel = int((df["cancelled"] == 1).sum())
        ctx.drops.flag("E06", n_cancel)

        # ---------------- R-06：唯一键去重 ----------------
        df["flight_sk"] = _flight_sk(df)
        dup_before = len(df)
        df = df.drop_duplicates(subset=["flight_date", "reporting_airline", "flight_number", "origin"],
                                keep="first")
        ctx.drops.add("E05", dup_before - len(df))

        # ---------------- 派生维度 ----------------
        df["year_month"] = df["flight_date_parsed"].dt.strftime("%Y-%m")
        df["flight_date"] = df["flight_date_parsed"].dt.strftime("%Y-%m-%d")
        df["day_of_week"] = df["flight_date_parsed"].dt.weekday
        df["dep_hour"] = (df["crs_dep_min"] // 60).clip(0, 23).astype("int64")
        df["dep_slot"] = (df["crs_dep_min"] // Config.CHINA_SLOT_MINUTES).clip(0, 47).astype("int64")
        df["route_key"] = df["origin"].astype(str) + "-" + df["dest"].astype(str)
        df["crs_dep_hhmm"] = (df["crs_dep_min"] % 1440).map(lambda m: f"{int(m)//60:02d}{int(m)%60:02d}")
        df["dep_hhmm"] = df["dep_min"].map(
            lambda m: None if pd.isna(m) else f"{int(m)%1440//60:02d}{int(m)%1440%60:02d}")

        # ---------------- 航班链重建（§5.3）----------------
        ctx.stage_start("flight_chain")
        df = _build_flight_chain(df, ctx)
        ctx.stage_end("flight_chain",
                      f"可重建链 {int(df['chain_available'].sum())} / {len(df)}")

        # ---------------- 气象时间窗对齐（§5.4）----------------
        ctx.stage_start("weather_join")
        df = _attach_weather(df, period, ctx)
        ctx.stage_end("weather_join",
                      f"weather_available=1 {int(df['weather_available'].sum())} 条")

        # ---------------- 写 DWD ----------------
        ctx.stage_start("write")
        result = engine.write("dwd_flight_detail", df, partition_value=period, mode="overwrite")
        ctx.rows_out = result["rows"]
        ctx.stage_end("write", f"{result['rows']} 行 / {result['files']} 文件")
        record_audit(ctx, f"dwd_{period}", period, "", 0, ctx.rows_in, result["rows"],
                     "success", 0.0)
        return ctx.finish("success")
    except Exception as exc:
        ctx.error("DWD 航班作业失败：%s", exc)
        import traceback
        ctx.error(traceback.format_exc())
        return ctx.finish("failed", "E_DWD_FLIGHT", str(exc))


def _build_flight_chain(df: pd.DataFrame, ctx: JobContext) -> pd.DataFrame:
    """按尾号排序取前序航段，计算过站时间与缓冲比。"""
    df = df.copy()
    df["chain_available"] = df["tail_number"].notna().astype(int)
    df.loc[df["tail_number"].astype("string").str.strip().isin(["", "NA"]), "chain_available"] = 0

    from app.services.stats import airport_dim
    airports = airport_dim()
    local_times = df["flight_date_parsed"] + pd.to_timedelta(df["crs_dep_min"], unit="m")
    df["_dep_ts"] = pd.to_datetime([
        local_departure_utc(ts, airports.get(origin, {}).get("tz"))
        for ts, origin in zip(local_times, df["origin"])])
    # 前序实际到达时刻 ≈ 计划起飞 + 计划飞行时长 + 到达延误
    eta_offset = df["crs_elapsed_min"] + df["arr_delay_min"]
    df["_arr_ts"] = df["_dep_ts"] + pd.to_timedelta(eta_offset, unit="m")

    chain = df[(df["chain_available"] == 1) & df["_dep_ts"].notna()].sort_values(
        ["tail_number", "_dep_ts"])
    if chain.empty:
        df["prev_flight_sk"] = None
        df["prev_arr_delay_min"] = np.nan
        df["turnaround_min"] = np.nan
        df["buffer_ratio"] = np.nan
        df["risk_flag"] = 0
        df["chain_available"] = 0
        return df

    grouped = chain.groupby("tail_number", sort=False)
    df["prev_flight_sk"] = pd.Series(None, index=df.index, dtype="object")
    df.loc[chain.index, "prev_flight_sk"] = grouped["flight_sk"].shift(1)
    df.loc[chain.index, "prev_arr_delay_min"] = grouped["arr_delay_min"].shift(1)
    prev_arr_ts = grouped["_arr_ts"].shift(1)
    prev_dest = grouped["dest"].shift(1)
    turnaround = (chain["_dep_ts"] - prev_arr_ts).dt.total_seconds() / 60.0
    # 过站 = 同一架飞机"上一段到达机场 == 本段起飞机场"时的地面停留时间；
    # 跨机场说明中间还有别的航段或已过夜，不能算过站（否则中位数被拉到 4 小时以上）。
    same_airport = prev_dest.astype(str).str.upper() == chain["origin"].astype(str).str.upper()
    previous_operated = grouped["cancelled"].shift(1).eq(0)
    turnaround = turnaround.where(same_airport & previous_operated & (turnaround > 10) & (turnaround < 720))
    df.loc[chain.index, "turnaround_min"] = turnaround.round(1)

    # 基准只从本航班之前的已知过站样本估计，禁止使用全月中位数。
    history = pd.DataFrame({"turn": turnaround, "airline": chain["reporting_airline"],
                            "ts": chain["_dep_ts"]}).sort_values("ts")
    base = history.groupby("airline")["turn"].transform(
        lambda s: s.expanding(min_periods=1).median().shift(1)).reindex(chain.index).replace(0, np.nan)
    ratio = (turnaround / base).replace([np.inf, -np.inf], np.nan)
    df.loc[chain.index, "buffer_ratio"] = ratio.round(4)

    df["chain_available"] = df["turnaround_min"].notna().astype(int)
    for col in ("prev_flight_sk", "prev_arr_delay_min", "turnaround_min", "buffer_ratio"):
        df.loc[df["chain_available"] == 0, col] = None if col == "prev_flight_sk" else np.nan
    df["risk_flag"] = (df["buffer_ratio"] < 1.0).astype(int)
    return df


def _attach_weather(df: pd.DataFrame, period: str, ctx: JobContext) -> pd.DataFrame:
    """取计划起飞前 1–3 小时气象窗口，缺失标 weather_available=0。"""
    year = period[:4]
    df = df.copy()
    try:
        anchors = df["_dep_ts"].dropna()
        years = sorted({year} | {str(t.year) for t in anchors}
                       | {str((t - pd.Timedelta(hours=3)).year) for t in anchors})
        weather = engine.read("dwd_weather_hourly", filters=[("year", "in", years)])
    except Exception as exc:
        ctx.warn("读取 %s 年气象失败（%s），全部标 weather_available=0", year, exc)
        weather = pd.DataFrame()

    if weather.empty:
        for col in ("vis_min_mi", "wind_max_ms", "tmp_mean_c", "dew_mean_c", "slp_mean_hpa",
                    "precip_mm", "weather_class"):
            df[col] = np.nan
        df["weather_available"] = 0
        ctx.drops.flag("E08", len(df))
        return df

    window = _window_weather_features(weather)
    window["anchor_hour"] = pd.to_datetime(window["anchor_hour"])
    # ODS 里 origin 是 pandas string dtype，窗口表是 object —— 直接 merge 会 0 命中，统一成 str
    window["origin"] = window["origin"].astype(str)
    df["origin"] = df["origin"].astype(str)
    df["anchor_hour"] = pd.to_datetime(df["_dep_ts"]).dt.floor("h")
    merged = df.merge(window, on=["origin", "anchor_hour"], how="left")
    merged.index = df.index
    missing = int(merged["weather_available"].isna().sum())
    merged["weather_available"] = merged["weather_available"].fillna(0).astype(int)
    if missing:
        ctx.drops.flag("E08", missing)
    merged["weather_class"] = merged["weather_class"].fillna(-1).astype(int)
    return merged


def run(periods: list[str], operator: str = "system", limit: int | None = None) -> dict:
    """按月跑：先补气象（年分区），再洗航班（月分区）。"""
    results = {}
    years = sorted({p[:4] for p in periods})
    for year in years:
        try:
            results[f"dwd_weather_hourly:{year}"] = build_dwd_weather(year, operator)
        except Exception as exc:
            results[f"dwd_weather_hourly:{year}"] = {"status": "failed", "err_msg": str(exc)}
    for period in periods:
        results[f"dwd_flight_detail:{period}"] = build_dwd_flight(period, operator, limit=limit)
    return results



