"""从代码自动生成文档：数据字典 + 接口清单。

用法：
    python scripts/gen_docs.py                  # 生成 docs/数据字典.md、docs/接口清单.md
    python scripts/gen_docs.py --with-server http://127.0.0.1:5000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.config import Config  # noqa: E402
from app.services.catalog import LAYERS, MODEL_FEATURES, TABLES, tables_by_layer  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent / "docs"

TYPE_CN = {"string": "字符串", "int": "整数", "float": "浮点", "bool": "布尔"}


def data_dictionary() -> str:
    lines = [
        "# 数据字典（四层十七表）",
        "",
        "> 本文件由 `scripts/gen_docs.py` 从 `app/services/catalog.py` 自动生成，勿手改。",
        "",
        f"- 数仓根目录：`{Config.WAREHOUSE_ROOT}`",
        "- 存储格式：Parquet + Snappy；分区目录采用 Hive 风格 `col=value/`",
        "- 分区列只体现在目录名，不写进数据文件（避免读取时类型冲突）",
        "",
        "## 一、表清单",
        "",
        "| 层 | 表名 | 粒度 | 主键 | 分区键 | 预估行数 | 预估体积 | 说明 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for layer in LAYERS:
        for table in tables_by_layer()[layer]:
            lines.append(
                f"| {layer.upper()} | `{table.name}` | {table.grain} | `{table.primary_key}` | "
                f"{('`' + table.partition_col + '`') if table.partition_col else '不分区'} | "
                f"{table.est_rows or '—'} | {table.est_size or '—'} | {table.description} |"
            )

    lines += ["", "## 二、字段明细", ""]
    for layer in LAYERS:
        lines.append(f"### {layer.upper()} 层")
        lines.append("")
        for table in tables_by_layer()[layer]:
            lines.append(f"#### `{table.name}`")
            lines.append("")
            lines.append(f"- 粒度：{table.grain}；主键：`{table.primary_key}`；"
                         f"分区：{('`' + table.partition_col + '`') if table.partition_col else '不分区'}")
            lines.append(f"- 说明：{table.description}")
            if table.tags:
                lines.append(f"- 标签：{' / '.join(table.tags)}")
            lines.append("")
            lines.append("| 字段 | 类型 | 中文名 |")
            lines.append("| --- | --- | --- |")
            for name, dtype in table.columns.items():
                cn = CN_NAMES.get(name, "")
                lines.append(f"| `{name}` | {TYPE_CN.get(dtype, dtype)} | {cn} |")
            lines.append("")

    lines += [
        "## 三、建模特征白名单（A-8 泄漏检查口径）",
        "",
        f"共 {len(MODEL_FEATURES)} 个特征；黑名单见 `Config.FEATURE_BLACKLIST`"
        "（延误原因分解 5 项 + 事后字段），二者交集必须为空。",
        "",
        "| # | 特征 | 中文名 |",
        "| --- | --- | --- |",
    ]
    for i, name in enumerate(MODEL_FEATURES, 1):
        lines.append(f"| {i} | `{name}` | {CN_NAMES.get(name, '')} |")
    lines.append("")
    return "\n".join(lines)


CN_NAMES = {
    # 通用维度
    "flight_date": "航班日期", "year_month": "年月分区", "day_of_week": "星期几（0=周一）",
    "dep_hour": "计划起飞小时", "dep_slot": "30 分钟时段序号", "month": "月份",
    "reporting_airline": "航司两字码", "tail_number": "飞机尾号", "flight_number": "航班号",
    "origin": "起飞机场 IATA", "dest": "到达机场 IATA", "route_key": "航线键 ORIGIN-DEST",
    "crs_dep_hhmm": "计划起飞时刻", "dep_hhmm": "实际起飞时刻",
    "crs_dep_min": "计划起飞当日分钟数", "dep_min": "实际起飞当日分钟数",
    "dep_delay_min": "起飞延误分钟（保留负值）", "dep_del15": "是否延误≥15 分钟",
    "arr_delay_min": "到达延误分钟", "arr_del15": "是否到达延误≥15 分钟",
    "dep_delay_check": "自算起飞延误（交叉校验）", "delay_check_diff": "双口径差异分钟",
    "delay_level": "延误等级 0/1/2（-1=取消）", "cancelled": "是否取消",
    "cancellation_code": "取消原因码", "diverted": "是否备降",
    "distance_mi": "航段距离（英里）", "crs_elapsed_min": "计划飞行时长（分钟）",
    "actual_elapsed_min": "实际飞行时长（分钟）", "air_time_min": "空中时间（分钟）",
    "taxi_out_min": "滑出时间（分钟）", "taxi_in_min": "滑入时间（分钟）",
    "chain_available": "能否重建航班链", "prev_flight_sk": "前序航班代理键",
    "prev_arr_delay_min": "前序航班到达延误", "turnaround_min": "同机场过站时间（分钟）",
    "buffer_ratio": "过站缓冲比（相对航司中位数）", "risk_flag": "缓冲不足风险标记",
    "weather_available": "气象特征是否可用", "vis_min_mi": "起飞前窗口最低能见度（英里）",
    "wind_max_ms": "起飞前窗口最大风速（m/s）", "tmp_mean_c": "起飞前窗口均温（℃）",
    "dew_mean_c": "起飞前窗口露点（℃）", "slp_mean_hpa": "起飞前窗口气压（hPa）",
    "precip_mm": "起飞前窗口降水（mm）", "weather_class": "7 类天气编码",
    "outlier": "延误值越界标记", "src_file": "来源文件名",
    "flight_sk": "航班代理键 md5", "is_weekend": "是否周末", "is_holiday": "是否美国联邦假日",
    "dow_sin": "星期正弦编码", "dow_cos": "星期余弦编码",
    "hour_sin": "小时正弦编码", "hour_cos": "小时余弦编码",
    "origin_traffic_24h": "起飞机场前一日航班量",
    "origin_traffic_prev_day": "起飞机场前一日航班量",
    "origin_delay_rate_prev_day": "起飞机场前一日延误率",
    "origin_delay_rate_7d": "起飞机场近 7 日延误率",
    "dest_delay_rate_7d": "到达机场近 7 日延误率",
    "route_delay_rate_7d": "航线近 7 日延误率",
    "airline_delay_rate_7d": "航司近 7 日延误率",
    # ODS
    "origin_city_name": "起飞机场城市", "dest_city_name": "到达机场城市",
    "crs_dep_time": "计划起飞原值", "dep_time": "实际起飞原值",
    "dep_delay": "起飞延误原值", "dep_delay_minutes": "起飞延误分钟原值",
    "crs_arr_time": "计划到达原值", "arr_time": "实际到达原值",
    "arr_delay": "到达延误原值", "arr_delay_minutes": "到达延误分钟原值",
    "taxi_out": "滑出原值", "taxi_in": "滑入原值", "air_time": "空中时间原值",
    "crs_elapsed_time": "计划时长原值", "actual_elapsed_time": "实际时长原值",
    "distance": "距离原值", "distance_group": "距离分组",
    "carrier_delay": "承运人原因延误（禁用为特征）",
    "weather_delay": "天气原因延误（禁用为特征）",
    "nas_delay": "空管/流量原因延误（禁用为特征）",
    "security_delay": "安保原因延误（禁用为特征）",
    "late_aircraft_delay": "前序航班晚到原因延误（禁用为特征）",
    "src_line": "源文件行号",
    # 气象
    "station_id": "ISD 站号（USAF+WBAN）", "station_name": "站点名称",
    "obs_time_utc": "观测时间 UTC", "obs_hour": "观测小时",
    "latitude": "纬度", "longitude": "经度", "elevation_m": "海拔（米）",
    "report_type": "报文类型", "source": "数据源编码", "wnd": "风编码串",
    "cig": "云底高编码串", "vis": "能见度编码串", "tmp": "气温编码串",
    "dew": "露点编码串", "slp": "气压编码串", "aa1": "降水编码串", "remarks": "备注/METAR",
    "airport_iata": "机场 IATA 码", "vis_mi": "能见度（英里）", "ceil_ft": "云底高（英尺）",
    "wind_ms": "风速（m/s）", "wind_dir": "风向（度）", "gust_ms": "阵风（m/s）",
    "temp_c": "气温（℃）", "dew_c": "露点（℃）", "slp_hpa": "气压（hPa）",
    # 维表
    "icao_code": "ICAO 四字码", "ident": "OurAirports 标识", "name": "名称",
    "city": "城市", "country": "国家", "iso_region": "省/州", "lat": "纬度", "lon": "经度",
    "elevation_ft": "海拔（英尺）", "tz": "时区", "airport_type": "机场类型",
    "scheduled_service": "是否有定期航班", "runway_count": "跑道数",
    "max_runway_ft": "最长跑道（英尺）", "is_us": "是否美国", "is_china": "是否中国",
    "in_bts": "是否出现在 BTS", "airline_code": "航司两字码", "icao": "ICAO 代码",
    "callsign": "呼号", "active": "是否运营中",
    "date_key": "日期键", "year": "年", "day": "日", "quarter": "季度", "weekday": "星期",
    "week_of_year": "年内周序", "holiday_name": "节日名", "region": "地区",
    "holiday_type": "节日类型", "is_observed": "是否补休",
    # 汇总/应用
    "flights": "航班量", "dep_del15_cnt": "延误航班数", "severe_cnt": "严重延误数",
    "cancelled_cnt": "取消数", "dep_delay_sum_min": "延误分钟合计",
    "dep_delay_avg_min": "平均延误分钟", "ontime_rate": "准点率", "cancel_rate": "取消率",
    "delay_rate": "延误率", "p90_dep_delay_min": "延误 P90（分钟）",
    "weather_available_cnt": "有气象样本数", "vis_mean_mi": "能见度均值",
    "wind_max_ms": "最大风速", "precip_sum_mm": "降水合计",
    "stat_date": "统计日期", "stat_hour": "统计小时", "airlines": "航司数",
    "prev_delay_cnt": "前序延误航班数", "chain_available_cnt": "可重建链航班数",
    "slot_id": "时间片 ID（日期#序号）", "slot_seq": "时间片序号",
    "slot_start_min": "时间片起始分钟", "risk_score": "综合风险分 0-100",
    "model_ver": "模型版本", "prob_delay": "延误概率", "pred_class": "二分类预测",
    "pred_level": "三级分类预测", "pred_minutes": "预测延误分钟", "risk_level": "风险等级",
    "top_factors": "Top 归因因素（JSON）", "actual_dep_delay_min": "真实起飞延误",
    "actual_dep_del15": "真实是否延误", "task": "任务", "split": "数据切分",
    "algo": "算法", "f1": "F1", "precision": "精确率", "recall": "召回率", "auc": "AUC",
    "accuracy": "准确率", "mae": "MAE", "rmse": "RMSE", "r2": "R²",
    "threshold": "判定阈值", "support": "样本量", "created_at": "创建时间",
    "metric": "指标名", "value": "指标值", "rank_no": "排名", "delta_pct": "环比变化(%)",
    "alert_id": "预警 ID", "alert_date": "预警日期", "rule": "规则", "observed": "观测值",
    "level": "级别", "message": "说明",
    # 天气事件
    "event_id": "事件 ID", "event_type": "事件类型", "severity": "严重度",
    "start_time_utc": "开始时间 UTC", "end_time_utc": "结束时间 UTC",
    "precipitation": "降水量（英寸）", "airport_code": "最近气象站代码",
    "county": "县", "state": "州", "zipcode": "邮编",
}


def api_listing(base: str | None) -> str:
    app = create_app()
    lines = [
        "# 接口清单",
        "",
        "> 本文件由 `scripts/gen_docs.py` 从 Flask `url_map` 自动生成，勿手改。",
        "",
        "统一响应结构：`{\"code\": 0, \"message\": \"success\", \"data\": {...}, \"trace_id\": \"...\"}`",
        "",
        "| code | 含义 |",
        "| --- | --- |",
        "| 0 | 成功 |",
        "| 400 / 422 | 参数缺失 / 取值非法 |",
        "| 401 / 403 | 未认证 / 角色不足 |",
        "| 404 | 资源不存在 |",
        "| 413 / 415 | 请求过大 / 类型不支持 |",
        "| 500 / 503 | 服务内部错误 / 依赖不可用 |",
        "| 521 / 522 | 数据未就绪 / 结果为空 |",
        "",
    ]
    groups: dict[str, list] = {}
    for rule in app.url_map.iter_rules():
        path = str(rule)
        if not path.startswith("/api"):
            continue
        prefix = path.split("/")[3] if len(path.split("/")) > 3 else "root"
        groups.setdefault(prefix, []).append(rule)

    for prefix in sorted(groups):
        lines.append(f"## /api/v1/{prefix}")
        lines.append("")
        lines.append("| 方法 | 路径 | 权限 | 说明 |")
        lines.append("| --- | --- | --- | --- |")
        for rule in sorted(groups[prefix], key=lambda r: str(r)):
            methods = ",".join(sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS")))
            view = app.view_functions[rule.endpoint]
            doc = (view.__doc__ or "").strip().splitlines()
            summary = doc[0] if doc else ""
            protected = getattr(view, "__wrapped__", None) is not None
            role = "登录" if protected else "公开/可选"
            if "require_role" in getattr(view, "__qualname__", ""):
                role = "受限"
            lines.append(f"| {methods} | `{str(rule)}` | {role} | {summary} |")
        lines.append("")

    lines += [
        "## 统一约定",
        "",
        "| 项 | 规范 |",
        "| --- | --- |",
        "| 版本前缀 | `/api/v1` |",
        "| 认证 | `Authorization: Bearer <JWT>`，有效期 8 小时 |",
        "| 分页 | `page` / `page_size`（≤200），返回 `total`、`pages` |",
        "| 时间入参 | `yyyy-MM-dd` / `yyyy-MM`（严格校验，非法返回 422） |",
        "| 缓存 | GET 默认 300s，看板 600s，响应头 `Cache-Control` 显式声明 |",
        "| 导出 | CSV（带 BOM），默认最多 1000 行，响应头 `X-Max-Rows` |",
        "| 追踪 | 每个响应带 `X-Trace-Id`，与响应体 `trace_id` 一致 |",
        "",
    ]
    if base:
        lines.append(f"在线查看：`{base}/api/v1/system/endpoints`")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--with-server", default=None)
    args = parser.parse_args()
    DOCS.mkdir(parents=True, exist_ok=True)

    dictionary = data_dictionary()
    (DOCS / "数据字典.md").write_text(dictionary, encoding="utf-8")
    print(f"已生成 docs/数据字典.md（{len(dictionary)} 字符）")

    listing = api_listing(args.with_server)
    (DOCS / "接口清单.md").write_text(listing, encoding="utf-8")
    print(f"已生成 docs/接口清单.md（{len(listing)} 字符）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
