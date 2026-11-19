"""态势看板接口：P1 总览 / P2 网络 / P3 排行 / P4 传播路径。"""
from __future__ import annotations

from flask import Blueprint

from app.api.common import cache_headers, csv_response
from app.services import stats
from app.utils import params
from app.utils.auth import optional_auth, require_role
from app.utils.response import ok

bp = Blueprint("dashboard", __name__, url_prefix="/api/v1")


def _scope_args() -> dict:
    return {
        "date_from": params.get_str("date_from"),
        "date_to": params.get_str("date_to"),
        "airports": params.get_list("airports", upper=True),
        "airlines": params.get_list("airlines", upper=True),
    }


# ---------------------------------------------------------------- P1 总览
@bp.get("/overview")
@optional_auth
@cache_headers(600)
def overview():
    args = _scope_args()
    kpi = stats.overview_kpi(args["date_from"], args["date_to"],
                             tuple(args["airports"]), tuple(args["airlines"]))
    trend = stats.overview_trend(args["date_from"], args["date_to"],
                                 params.get_str("granularity", "day", choices=["day", "month"]),
                                 tuple(args["airports"]), tuple(args["airlines"]))
    return ok({"kpi": kpi, "trend": trend})


@bp.get("/overview/kpi")
@optional_auth
@cache_headers(600)
def overview_kpi():
    args = _scope_args()
    return ok(stats.overview_kpi(args["date_from"], args["date_to"],
                                 tuple(args["airports"]), tuple(args["airlines"])))


@bp.get("/overview/trend")
@optional_auth
@cache_headers(600)
def overview_trend():
    args = _scope_args()
    return ok(stats.overview_trend(args["date_from"], args["date_to"],
                                   params.get_str("granularity", "day", choices=["day", "month"]),
                                   tuple(args["airports"]), tuple(args["airlines"])))


@bp.get("/overview/distribution")
@optional_auth
@cache_headers(600)
def overview_distribution():
    return ok(stats.delay_distribution(params.get_str("date_from"), params.get_str("date_to"),
                                       tuple(params.get_list("airports", upper=True))))


@bp.get("/overview/reasons")
@optional_auth
@cache_headers(600)
def overview_reasons():
    args = _scope_args()
    return ok(stats.delay_reasons(args["date_from"], args["date_to"],
                                  tuple(args["airports"]), tuple(args["airlines"])))


# ---------------------------------------------------------------- P2 网络
@bp.get("/network")
@optional_auth
@cache_headers(600)
def network():
    return ok(stats.network_graph(
        params.get_str("date_from"), params.get_str("date_to"),
        params.get_str("metric", "delay_rate", choices=["delay_rate", "ontime_rate", "cancel_rate", "avg_delay_min"]),
        params.get_int("top_routes", 300, minimum=1, maximum=2000),
        params.get_int("min_flights", 1, minimum=1, maximum=1000),
        tuple(params.get_list("airports", upper=True)),
    ))


@bp.get("/network/heatmap")
@optional_auth
@cache_headers(600)
def network_heatmap():
    return ok(stats.delay_heatmap(
        params.get_str("date_from"), params.get_str("date_to"),
        params.get_int("top", 20, minimum=1, maximum=50),
        params.get_str("metric", "delay_rate",
                       choices=["delay_rate", "avg_delay_min", "cancel_rate", "flights"]),
    ))


# ---------------------------------------------------------------- P3 排行与详情
@bp.get("/airport/rank")
@optional_auth
@cache_headers(600)
def airport_rank():
    return ok(stats.airport_rank(
        params.get_str("stat_date"),
        params.get_str("metric", "delay_rate", choices=list(stats.RANK_METRICS)),
        params.get_int("top", 20, minimum=1, maximum=100),
        params.get_str("order", "desc", choices=["desc", "asc"]),
    ))


@bp.get("/airport/<iata>")
@optional_auth
@cache_headers(600)
def airport_profile(iata: str):
    if len(iata) != 3:
        from app.utils.response import ApiError, ErrCode
        raise ApiError(ErrCode.PARAM_INVALID, "机场代码必须是 3 位 IATA 码")
    return ok(stats.airport_profile(iata, params.get_str("date_from"), params.get_str("date_to")))


@bp.get("/airport/<iata>/rank")
@optional_auth
def airport_routes(iata: str):
    """机场下钻：该机场进出港航线排行（复用 profile 的航线部分）。"""
    data = stats.airport_profile(iata, params.get_str("date_from"), params.get_str("date_to"))
    return ok({"airport": data["airport"], "routes": data["top_routes"]})


# ---------------------------------------------------------------- P4 传播路径
@bp.get("/propagation")
@require_role("ops")
def propagation():
    return ok(stats.propagation(
        params.get_str("stat_date"),
        params.get_str("airport"),
        params.get_int("limit", 2000, minimum=100, maximum=20000),
    ))


# ---------------------------------------------------------------- 导出
# ---------------------------------------------------------------- 跨域（中国域）
@bp.get("/crossdomain/china")
@optional_auth
@cache_headers(600)
def crossdomain_china():
    """中国域：机场 × 30 分钟延误指标（STPN，2015-04 ~ 2017-05）。"""
    return ok(stats.crossdomain_china(
        params.get_int("top", 50, minimum=1, maximum=50),
        params.get_str("date_from"), params.get_str("date_to"),
    ))


@bp.get("/crossdomain/compare")
@optional_auth
@cache_headers(600)
def crossdomain_compare():
    """美国域（BTS 聚合到机场×30 分钟）与中国域（STPN）的指标对照。"""
    return ok(stats.crossdomain_compare(params.get_str("period")))


@bp.get("/overview/export")
@optional_auth
def export_overview():
    args = _scope_args()
    kpi = stats.overview_kpi(args["date_from"], args["date_to"],
                             tuple(args["airports"]), tuple(args["airlines"]))
    rows = [{"指标": k, "取值": v} for k, v in kpi["kpi"].items()]
    return csv_response(rows, "overview_kpi")

