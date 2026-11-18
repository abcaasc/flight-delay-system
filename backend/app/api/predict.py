"""预测与归因接口（P7 + 功能模块 6）。"""
from __future__ import annotations

from flask import Blueprint

from app.services import predict_service
from app.utils import params
from app.utils.auth import optional_auth, require_role
from app.utils.response import ApiError, ErrCode, ok

bp = Blueprint("predict", __name__, url_prefix="/api/v1")


@bp.post("/predict")
@optional_auth
def predict_one():
    body = params.json_body()
    origin = params.body_str(body, "origin", required=True, max_len=3).upper()
    dest = params.body_str(body, "dest", required=True, max_len=3).upper()
    airline = params.body_str(body, "airline", required=True, max_len=3).upper()
    flight_date = params.body_str(body, "flight_date", required=True, max_len=10)
    crs_dep_time = params.body_str(body, "crs_dep_time", required=True, max_len=4)
    distance = body.get("distance_mi")

    for code, name in ((origin, "origin"), (dest, "dest")):
        if len(code) != 3 or not code.isascii() or not code.isalpha():
            raise ApiError(ErrCode.PARAM_INVALID, f"{name} 必须是 3 位 IATA 码")
    if origin == dest:
        raise ApiError(ErrCode.PARAM_INVALID, "起降机场不能相同")

    try:
        result = predict_service.predict_single(
            origin, dest, airline, flight_date, crs_dep_time,
            float(distance) if distance not in (None, "") else None,
            top_k=params.body_int(body, "top_k", default=10, minimum=1, maximum=28) or 10,
        )
    except (ValueError, TypeError, OverflowError) as exc:
        raise ApiError(ErrCode.PARAM_INVALID, str(exc))
    return ok(result)


@bp.get("/predict/high-risk")
@optional_auth
def high_risk():
    try:
        data = predict_service.high_risk_flights(
            params.get_str("period"),
            params.get_int("top", 20, minimum=1, maximum=200),
            params.get_float("min_prob", 0.5, minimum=0.0, maximum=1.0),
        )
    except ValueError as exc:
        raise ApiError(ErrCode.DATA_NOT_READY, str(exc))
    return ok(data)


@bp.get("/predict/airport")
@optional_auth
def airport_prediction():
    try:
        data = predict_service.airport_prediction(params.get_str("period"),
                                                  params.get_int("top", 20, minimum=1, maximum=50))
    except ValueError as exc:
        raise ApiError(ErrCode.DATA_NOT_READY, str(exc))
    return ok(data)


@bp.get("/explain/global")
@optional_auth
def global_importance():
    return ok(predict_service.global_importance(params.get_str("period"),
                                                params.get_int("top", 20, minimum=1, maximum=40)))


@bp.get("/explain/<flight_sk>")
@require_role("ops")
def explain(flight_sk: str):
    try:
        data = predict_service.explain_flight(flight_sk,
                                              params.get_int("top_k", 10, minimum=1, maximum=28))
    except ValueError as exc:
        raise ApiError(ErrCode.NOT_FOUND, str(exc))
    return ok(data)
