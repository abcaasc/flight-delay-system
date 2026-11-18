"""交互查询接口：多条件筛选、结果导出（开题报告功能模块 5）。"""
from __future__ import annotations

from flask import Blueprint

from app.api.common import csv_response, no_cache
from app.config import Config
from app.services import stats
from app.utils import params
from app.utils.auth import optional_auth, write_audit
from app.utils.response import ok

bp = Blueprint("flights", __name__, url_prefix="/api/v1/flights")


def _filters() -> dict:
    return {
        "date_from": params.get_str("date_from"),
        "date_to": params.get_str("date_to"),
        "origin": params.get_str("origin"),
        "dest": params.get_str("dest"),
        "airline": params.get_str("airline"),
        "min_delay": params.get_float("min_delay", None, minimum=-300, maximum=1500),
        "max_delay": params.get_float("max_delay", None, minimum=-300, maximum=1500),
        "cancelled": (None if params.get_str("cancelled") is None
                      else params.get_bool("cancelled")),
    }


@bp.get("")
@optional_auth
@no_cache
def query():
    page, size = params.page_args()
    args = _filters()
    result = stats.flights_query(
        args["date_from"], args["date_to"], args["origin"], args["dest"], args["airline"],
        args["min_delay"], args["max_delay"], args["cancelled"],
        page=page, page_size=size,
        sort_by=params.get_str("sort_by", "dep_delay_min",
                               choices=["dep_delay_min", "arr_delay_min", "flight_date",
                                        "crs_dep_hhmm", "distance_mi", "prev_arr_delay_min"]),
        order=params.get_str("order", "desc", choices=["desc", "asc"]),
    )
    return ok(result)


@bp.get("/export")
@optional_auth
def export():
    args = _filters()
    result = stats.flights_query(
        args["date_from"], args["date_to"], args["origin"], args["dest"], args["airline"],
        args["min_delay"], args["max_delay"], args["cancelled"],
        limit=Config.EXPORT_MAX_ROWS,
    )
    write_audit("export", "dwd_flight_detail",
                f"导出 {len(result['items'])} 行，条件 {result['filters']}")
    rows = [{
        "航班日期": r.get("flight_date"),
        "航司": r.get("reporting_airline"),
        "航班号": r.get("flight_number"),
        "起飞机场": r.get("origin"),
        "到达机场": r.get("dest"),
        "计划起飞": r.get("crs_dep_hhmm"),
        "实际起飞": r.get("dep_hhmm"),
        "起飞延误(分钟)": r.get("dep_delay_min"),
        "到达延误(分钟)": r.get("arr_delay_min"),
        "是否延误>15min": r.get("dep_del15"),
        "延误等级": r.get("delay_level"),
        "是否取消": r.get("cancelled"),
        "距离(英里)": r.get("distance_mi"),
        "前序延误(分钟)": r.get("prev_arr_delay_min"),
        "过站(分钟)": r.get("turnaround_min"),
        "能见度(英里)": r.get("vis_min_mi"),
        "风速(m/s)": r.get("wind_max_ms"),
        "航班代理键": r.get("flight_sk"),
    } for r in result["items"]]
    return csv_response(rows, "flights")


@bp.get("/<flight_sk>")
@optional_auth
def detail(flight_sk: str):
    return ok(stats.flight_detail(flight_sk))
