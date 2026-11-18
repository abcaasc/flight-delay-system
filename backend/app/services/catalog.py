"""数仓表目录（四层十七表）—— ODS/DIM/DWD/DWS/ADS 的唯一契约来源。

ETL 写入、服务层读取、表浏览接口全部引用本文件，避免"字段名各处写一遍"。
字段类型用 pyarrow/pandas 可理解的字符串：string / int / float / bool。

与《数据接入设计说明》§三 Hive DDL 的差异（均已在表注释中说明原因）：
  * ods_isd_weather 用结构化字段替代整行 raw_line —— 整行保真会让 1142 个站点
    文件膨胀到 ~10GB，而 raw 区已保留原始 CSV 作为唯一保真副本。
  * ods_bts_flight 在 DDL 的 25 列基础上补 10 列（crs_arr_time/arr_time/
    taxi_out/taxi_in/air_time/crs_elapsed_time/actual_elapsed_time/dep_del15/
    arr_del15/distance_group），它们既是 DWD 必需，也便于口径交叉校验。
"""
from __future__ import annotations

from dataclasses import dataclass, field

LAYERS = ("ods", "dim", "dwd", "dws", "ads")


@dataclass(frozen=True)
class TableDef:
    name: str
    layer: str
    grain: str
    primary_key: str
    partition_col: str | None
    columns: dict[str, str]
    description: str = ""
    # 预估规模，用于表浏览页面
    est_rows: str = ""
    est_size: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)

    @property
    def column_names(self) -> list[str]:
        return list(self.columns)


def _t(name: str, layer: str, grain: str, pk: str, part: str | None,
       cols: dict[str, str], desc: str = "", rows: str = "", size: str = "",
       tags: tuple[str, ...] = ()) -> TableDef:
    return TableDef(name=name, layer=layer, grain=grain, primary_key=pk, partition_col=part,
                    columns=cols, description=desc, est_rows=rows, est_size=size, tags=tags)


# ---------------------------------------------------------------- ODS
ODS_BTS_FLIGHT = _t(
    "ods_bts_flight", "ods", "一条原始航班记录", "file_name + src_line", "year_month",
    {
        "flight_date": "string", "reporting_airline": "string", "tail_number": "string",
        "flight_number": "string", "origin": "string", "origin_city_name": "string",
        "dest": "string", "dest_city_name": "string",
        "crs_dep_time": "string", "dep_time": "string", "dep_delay": "string",
        "dep_delay_minutes": "string", "dep_del15": "string",
        "crs_arr_time": "string", "arr_time": "string", "arr_delay": "string",
        "arr_delay_minutes": "string", "arr_del15": "string",
        "taxi_out": "string", "taxi_in": "string", "air_time": "string",
        "crs_elapsed_time": "string", "actual_elapsed_time": "string",
        "cancelled": "string", "cancellation_code": "string", "diverted": "string",
        "distance": "string", "distance_group": "string",
        "carrier_delay": "string", "weather_delay": "string", "nas_delay": "string",
        "security_delay": "string", "late_aircraft_delay": "string",
        "src_file": "string", "src_line": "int",
    },
    "BTS 航班准点 ODS，关键字段全字符串保真（原始 zip 另存 raw 区）",
    "6500 万", "3.0 GB", ("入口", "主表"),
)

ODS_ISD_WEATHER = _t(
    "ods_isd_weather", "ods", "一次逐时地面观测", "station_id + obs_time_utc", "year",
    {
        "station_id": "string", "station_name": "string", "obs_time_utc": "string",
        "latitude": "float", "longitude": "float", "elevation_m": "float",
        "report_type": "string", "source": "string",
        "wnd": "string", "cig": "string", "vis": "string", "tmp": "string",
        "dew": "string", "slp": "string", "aa1": "string", "remarks": "string",
        "src_file": "string",
    },
    "NOAA ISD 逐时观测 ODS，气象要素保留 NOAA 原始编码串（如 +0150,1）",
    "约 1000 万", "1.5 GB", ("入口", "气象"),
)

ODS_WEATHER_EVENT = _t(
    "ods_weather_event", "ods", "一次天气事件", "event_id", "year",
    {
        "event_id": "string", "event_type": "string", "severity": "string",
        "start_time_utc": "string", "end_time_utc": "string",
        "precipitation": "string", "airport_code": "string",
        "lat": "float", "lon": "float", "city": "string", "county": "string",
        "state": "string", "zipcode": "string",
    },
    "US Weather Events 2016-2022，用于气象极端事件叠加",
    "862 万", "0.4 GB", ("入口", "气象"),
)

# ---------------------------------------------------------------- DIM
DIM_AIRPORT = _t(
    "dim_airport", "dim", "一个机场", "airport_iata", None,
    {
        "airport_iata": "string", "icao_code": "string", "ident": "string", "name": "string",
        "city": "string", "country": "string", "iso_region": "string",
        "lat": "float", "lon": "float", "elevation_ft": "float", "tz": "string",
        "airport_type": "string", "scheduled_service": "string",
        "runway_count": "int", "max_runway_ft": "float",
        "is_us": "bool", "is_china": "bool", "in_bts": "bool",
    },
    "OurAirports 机场维表（含 ICAO 与气象站映射用 ident），带跑道容量特征",
    "8 万", "20 MB", ("维表", "广播"),
)

DIM_AIRLINE = _t(
    "dim_airline", "dim", "一个航司", "airline_code", None,
    {
        "airline_code": "string", "name": "string", "icao": "string", "callsign": "string",
        "country": "string", "active": "bool",
    },
    "OpenFlights 航司维表（ODbL，论文需署名）",
    "1500", "<1 MB", ("维表", "广播"),
)

DIM_DATE = _t(
    "dim_date", "dim", "一天", "date_key", None,
    {
        "date_key": "string", "year": "int", "month": "int", "day": "int",
        "quarter": "int", "weekday": "int", "week_of_year": "int",
        "is_weekend": "bool", "is_holiday": "bool", "holiday_name": "string",
        "year_month": "string",
    },
    "日期维表（含美国法定节假日，供节假日特征）",
    "3653", "<1 MB", ("维表",),
)

DIM_HOLIDAY = _t(
    "dim_holiday", "dim", "一天×地区", "date_key + region", None,
    {
        "date_key": "string", "region": "string", "holiday_name": "string",
        "holiday_type": "string", "is_observed": "bool",
    },
    "节假日维表（美国联邦假日；中国域保留 region=CN 占位）",
    "500", "<1 MB", ("维表",),
)

# ---------------------------------------------------------------- DWD
DWD_FLIGHT_DETAIL = _t(
    "dwd_flight_detail", "dwd", "一个有效航班（含航班链与气象特征）", "flight_sk", "year_month",
    {
        "flight_sk": "string", "flight_date": "string", "year_month": "string",
        "day_of_week": "int", "dep_hour": "int", "dep_slot": "int",
        "reporting_airline": "string", "tail_number": "string", "flight_number": "string",
        "origin": "string", "dest": "string", "route_key": "string",
        "crs_dep_hhmm": "string", "dep_hhmm": "string",
        "crs_dep_min": "int", "dep_min": "int",
        "dep_delay_min": "float", "dep_del15": "int", "arr_delay_min": "float", "arr_del15": "int",
        "dep_delay_check": "float", "delay_check_diff": "float", "delay_level": "int",
        "cancelled": "int", "cancellation_code": "string", "diverted": "int",
        "distance_mi": "float", "crs_elapsed_min": "float",
        "actual_elapsed_min": "float", "air_time_min": "float",
        "taxi_out_min": "float", "taxi_in_min": "float",
        "chain_available": "int", "prev_flight_sk": "string", "prev_arr_delay_min": "float",
        "turnaround_min": "float", "buffer_ratio": "float", "risk_flag": "int",
        "weather_available": "int", "vis_min_mi": "float", "wind_max_ms": "float",
        "tmp_mean_c": "float", "dew_mean_c": "float", "slp_mean_hpa": "float",
        "precip_mm": "float", "weather_class": "int",
        "outlier": "int", "src_file": "string",
    },
    "DWD 明细层：清洗 + 航班链重建 + 气象时间窗对齐（特征来源表）",
    "6200 万", "4.5 GB", ("核心", "宽表"),
)

DWD_WEATHER_HOURLY = _t(
    "dwd_weather_hourly", "dwd", "机场×小时天气", "airport_iata + obs_hour", "year",
    {
        "airport_iata": "string", "station_id": "string", "obs_hour": "string",
        "obs_time_utc": "string", "vis_mi": "float", "ceil_ft": "float",
        "wind_ms": "float", "wind_dir": "float", "gust_ms": "float",
        "temp_c": "float", "dew_c": "float", "slp_hpa": "float", "precip_mm": "float",
        "weather_class": "int", "weather_available": "int",
    },
    "机场×小时气象（ISD 编码串解析 + 机场↔站点映射 + 7 类天气编码）",
    "约 1.1 亿", "1.8 GB", ("气象",),
)

# ---------------------------------------------------------------- DWS
DWS_AIRPORT_HOURLY = _t(
    "dws_airport_hourly", "dws", "机场×小时", "airport_iata + stat_date + stat_hour", "year_month",
    {
        "airport_iata": "string", "stat_date": "string", "stat_hour": "int", "year_month": "string",
        "flights": "int", "dep_del15_cnt": "int", "severe_cnt": "int", "cancelled_cnt": "int",
        "dep_delay_sum_min": "float", "dep_delay_avg_min": "float",
        "ontime_rate": "float", "cancel_rate": "float", "delay_rate": "float",
        "p90_dep_delay_min": "float",
        "weather_available_cnt": "int", "vis_mean_mi": "float", "wind_max_ms": "float",
        "tmp_mean_c": "float", "precip_sum_mm": "float",
    },
    "看板 P1/P3 主力表：机场×小时准点率、延误率、取消率与气象叠加",
    "870 万", "0.3 GB", ("看板", "预聚合"),
)

DWS_ROUTE_DAILY = _t(
    "dws_route_daily", "dws", "航线×天", "route_key + stat_date", "year_month",
    {
        "route_key": "string", "origin": "string", "dest": "string",
        "stat_date": "string", "year_month": "string",
        "flights": "int", "airlines": "int", "dep_del15_cnt": "int", "cancelled_cnt": "int",
        "dep_delay_sum_min": "float", "dep_delay_avg_min": "float",
        "ontime_rate": "float", "cancel_rate": "float",
        "distance_mi": "float", "prev_delay_cnt": "int", "chain_available_cnt": "int",
    },
    "看板 P2 主力表：航线×天流量与准点率（航线网络图连边依据）",
    "2200 万", "0.8 GB", ("看板", "预聚合"),
)

DWS_DELAY_NETWORK = _t(
    "dws_delay_network", "dws", "机场×30 分钟时间片", "airport_iata + slot_id", "year",
    {
        "airport_iata": "string", "slot_id": "string", "stat_date": "string", "year": "int",
        "slot_seq": "int", "slot_start_min": "int",
        "flights": "int", "dep_del15_cnt": "int", "dep_delay_avg_min": "float",
        "ontime_rate": "float", "weather_class": "int", "risk_score": "float",
    },
    "图模型/跨域对齐用的机场×30 分钟时间片（与 STPN 中国域同粒度，S-09）",
    "5200 万", "1.2 GB", ("图模型", "跨域"),
)

DWS_FLIGHT_FEATURES = _t(
    "dws_flight_features", "dws", "一个航班（特征宽表）", "flight_sk", "year_month",
    {
        "flight_sk": "string", "year_month": "string", "flight_date": "string",
        "origin": "string", "dest": "string", "route_key": "string",
        "reporting_airline": "string", "dep_hour": "int", "dep_slot": "int",
        "month": "int", "day_of_week": "int", "is_weekend": "int", "is_holiday": "int",
        "dow_sin": "float", "dow_cos": "float", "hour_sin": "float", "hour_cos": "float",
        "distance_mi": "float", "crs_elapsed_min": "float",
        "origin_traffic_24h": "float", "origin_delay_rate_prev_day": "float",
        "origin_delay_rate_7d": "float", "dest_delay_rate_7d": "float",
        "route_delay_rate_7d": "float", "airline_delay_rate_7d": "float",
        "chain_available": "int", "prev_arr_delay_min": "float",
        "turnaround_min": "float", "buffer_ratio": "float", "risk_flag": "int",
        "weather_available": "int", "vis_min_mi": "float", "wind_max_ms": "float",
        "tmp_mean_c": "float", "precip_mm": "float", "weather_class": "int",
        "dep_delay_min": "float", "dep_del15": "int", "delay_level": "int",
    },
    "建模特征宽表：时间/网络/航班链/气象四组特征 + 三个标签；标签列不回灌特征",
    "6200 万", "5.0 GB", ("建模", "宽表"),
)

# ---------------------------------------------------------------- ADS
ADS_DELAY_PRED = _t(
    "ads_delay_pred", "ads", "一个航班×一个模型版本", "flight_sk + model_ver", "year_month",
    {
        "flight_sk": "string", "model_ver": "string", "year_month": "string", "flight_date": "string",
        "origin": "string", "dest": "string", "reporting_airline": "string", "dep_hour": "int",
        "prob_delay": "float", "pred_class": "int", "pred_level": "int",
        "pred_minutes": "float", "risk_level": "string", "top_factors": "string",
        "actual_dep_delay_min": "float", "actual_dep_del15": "int",
    },
    "预测结果表：延误概率、三级分类、延误分钟回归 + Top 归因因素（JSON 串）",
    "视测试集", "0.2 GB", ("预测", "结果"),
)

ADS_MODEL_METRIC = _t(
    "ads_model_metric", "ads", "一个模型×一个任务×一个切分", "model_ver + task + split", None,
    {
        "model_ver": "string", "task": "string", "split": "string", "algo": "string",
        "f1": "float", "precision": "float", "recall": "float", "auc": "float",
        "accuracy": "float", "mae": "float", "rmse": "float", "r2": "float",
        "threshold": "float", "support": "int", "created_at": "string",
    },
    "模型评估指标（分类看 F1/AUC，回归看 MAE/RMSE），供 P5 模型对比与 P6 泛化矩阵",
    "数百", "<1 MB", ("模型", "评估"),
)

ADS_AIRPORT_RANK = _t(
    "ads_airport_rank", "ads", "机场×统计日×指标", "stat_date + airport_iata + metric", "year_month",
    {
        "stat_date": "string", "year_month": "string", "airport_iata": "string",
        "metric": "string", "value": "float", "rank_no": "int", "delta_pct": "float",
        "flights": "int",
    },
    "机场排行（延误率/准点率/取消率/平均延误），含环比 delta，供 P3 排行页",
    "数十万", "50 MB", ("看板", "排行"),
)

ADS_ANOMALY_ALERT = _t(
    "ads_anomaly_alert", "ads", "一次预警事件", "alert_id", "year_month",
    {
        "alert_id": "string", "alert_date": "string", "year_month": "string",
        "airport_iata": "string", "rule": "string", "observed": "float",
        "threshold": "float", "level": "string", "message": "string",
    },
    "异常预警（延误率突增/取消率突增），供告警订阅与 P8 监控联动",
    "数千", "<1 MB", ("告警",),
)

TABLES: dict[str, TableDef] = {
    t.name: t for t in (
        ODS_BTS_FLIGHT, ODS_ISD_WEATHER, ODS_WEATHER_EVENT,
        DIM_AIRPORT, DIM_AIRLINE, DIM_DATE, DIM_HOLIDAY,
        DWD_FLIGHT_DETAIL, DWD_WEATHER_HOURLY,
        DWS_AIRPORT_HOURLY, DWS_ROUTE_DAILY, DWS_DELAY_NETWORK, DWS_FLIGHT_FEATURES,
        ADS_DELAY_PRED, ADS_MODEL_METRIC, ADS_AIRPORT_RANK, ADS_ANOMALY_ALERT,
    )
}


def get_table(name: str) -> TableDef:
    if name not in TABLES:
        raise KeyError(f"未知数仓表：{name}，可选：{sorted(TABLES)}")
    return TABLES[name]


def tables_by_layer() -> dict[str, list[TableDef]]:
    out: dict[str, list[TableDef]] = {layer: [] for layer in LAYERS}
    for table in TABLES.values():
        out[table.layer].append(table)
    return out


# ETL 依赖顺序（入湖 → 明细 → 汇总 → 应用）
ETL_ORDER = (
    "dim_airport", "dim_airline", "dim_date", "dim_holiday",
    "ods_bts_flight", "ods_isd_weather", "ods_weather_event",
    "dwd_weather_hourly", "dwd_flight_detail",
    "dws_airport_hourly", "dws_route_daily", "dws_delay_network", "dws_flight_features",
    "ads_airport_rank", "ads_anomaly_alert", "ads_delay_pred", "ads_model_metric",
)

# 建模可用特征（白名单）——A-8「泄漏检查：0 个禁用字段」以此为准
MODEL_FEATURES = (
    "dep_hour", "dep_slot", "month", "day_of_week", "is_weekend", "is_holiday",
    "dow_sin", "dow_cos", "hour_sin", "hour_cos",
    "distance_mi", "crs_elapsed_min",
    "origin_traffic_24h", "origin_delay_rate_prev_day", "origin_delay_rate_7d",
    "dest_delay_rate_7d", "route_delay_rate_7d", "airline_delay_rate_7d",
    "chain_available", "prev_arr_delay_min", "turnaround_min", "buffer_ratio", "risk_flag",
    "weather_available", "vis_min_mi", "wind_max_ms", "tmp_mean_c", "precip_mm",
)
MODEL_LABELS = ("dep_del15", "delay_level", "dep_delay_min")

