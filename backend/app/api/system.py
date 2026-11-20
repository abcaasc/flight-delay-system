"""系统管理接口：健康检查、审计日志、用户、告警规则、作业监控。"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint

from app.config import Config
from app.extensions import db
from app.models import AlertEvent, AlertRule, AuditLog, User
from app.services import engine, ingest, stats
from app.services.cache import cache_stats, invalidate
from app.services.spark_session import engine_info
from app.utils import params
from app.utils.auth import (
    ROLE_MENUS, ROLE_NAMES, current_identity, hash_password, optional_auth,
    require_role, write_audit,
)
from app.utils.response import ApiError, ErrCode, ok

bp = Blueprint("system", __name__, url_prefix="/api/v1/system")


# ---------------------------------------------------------------- 健康与可观测
@bp.get("/health")
@optional_auth
def health():
    tables = {name: engine.get_engine().exists(name) for name in engine.TABLES}
    ready = sum(1 for v in tables.values() if v)
    return ok({
        "status": "ok" if ready else "empty",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "env": Config.ENV,
        "engine": {"query": engine.get_engine().name, "spark": engine_info()},
        "cache": cache_stats(),
        "warehouse": {"ready_tables": ready, "total_tables": len(tables), "tables": tables},
        "data_root": str(Config.DATA_ROOT),
        "data_root_exists": Config.DATA_ROOT.exists(),
        "active_jobs": ingest.active_jobs(),
        "latest_period": engine.BaseEngine.latest_period("dws_airport_hourly"),
    })


@bp.get("/freshness")
@optional_auth
def freshness():
    """数据新鲜度（§6.6.3：右上角"数据截至 … · 更新于 N 分钟前"）。"""
    latest = engine.BaseEngine.latest_period("dws_airport_hourly")
    jobs = ingest.job_list(limit=5)
    last_success = next((j for j in jobs if j["status"] == "success"), None)
    return ok({
        "latest_period": latest,
        "latest_job": last_success,
        "tables": {
            name: {"period": engine.BaseEngine.latest_period(name),
                   "ready": engine.get_engine().exists(name)}
            for name in ("ods_bts_flight", "dwd_flight_detail", "dws_airport_hourly",
                         "ads_airport_rank", "ads_delay_pred")
        },
    })


@bp.get("/coverage")
@optional_auth
def coverage():
    """返回当前可选月份和逐班明细的覆盖情况。"""
    months = set(engine.BaseEngine.available_periods("dws_airport_hourly"))
    return ok({
        "dashboard": stats.month_coverage(months, "dws_airport_hourly", "dws_route_daily"),
        "flights": stats.month_coverage(
            months.union(engine.BaseEngine.available_periods("dwd_flight_detail")), "dwd_flight_detail"),
    })


@bp.get("/job/stats")
@require_role("ops")
def job_stats():
    return ok(ingest.job_stats(params.get_int("days", 30, minimum=1, maximum=365)))


@bp.get("/audit")
@require_role("admin")
def audit_log():
    page, size = params.page_args()
    query = db.session.query(AuditLog)
    if params.get_str("action"):
        query = query.filter(AuditLog.action == params.get_str("action"))
    if params.get_str("username"):
        query = query.filter(AuditLog.username == params.get_str("username"))
    total = query.count()
    rows = query.order_by(AuditLog.id.desc()).offset((page - 1) * size).limit(size).all()
    return ok({"total": total, "page": page, "page_size": size,
               "items": [r.to_dict() for r in rows]})


@bp.post("/cache/flush")
@require_role("admin")
def flush_cache():
    namespaces = ["overview", "network", "airport", "route", "flight", "pred", "quality"]
    removed = {ns: invalidate(ns) for ns in namespaces}
    return ok({"removed": removed, "cache": cache_stats()})


# ---------------------------------------------------------------- 用户管理
@bp.get("/users")
@require_role("admin")
def list_users():
    rows = db.session.query(User).order_by(User.id).all()
    return ok({"items": [r.to_dict() for r in rows], "roles": ROLE_NAMES,
               "menus": ROLE_MENUS})


@bp.post("/users")
@require_role("admin")
def create_user():
    body = params.json_body()
    username = params.body_str(body, "username", required=True, max_len=64)
    password = params.body_str(body, "password", required=True, max_len=128)
    role = params.body_str(body, "role", default="guest", max_len=16)
    if role not in ROLE_NAMES:
        raise ApiError(ErrCode.PARAM_INVALID, f"role 只能是 {list(ROLE_NAMES)}")
    if len(password) < 6:
        raise ApiError(ErrCode.PARAM_INVALID, "口令至少 6 位")
    if db.session.query(User).filter_by(username=username).first():
        raise ApiError(ErrCode.PARAM_INVALID, f"用户名已存在：{username}")
    user = User(username=username, password_hash=hash_password(password), role=role,
                display_name=params.body_str(body, "display_name", default=username, max_len=64),
                email=params.body_str(body, "email", default="", max_len=128))
    db.session.add(user)
    db.session.commit()
    write_audit("user_create", "sys_user", f"{username} role={role}")
    return ok(user.to_dict())


@bp.put("/users/<int:user_id>")
@require_role("admin")
def update_user(user_id: int):
    user = db.session.query(User).filter_by(id=user_id).first()
    if user is None:
        raise ApiError(ErrCode.NOT_FOUND, "用户不存在")
    body = params.json_body()
    if "role" in body:
        role = str(body["role"])
        if role not in ROLE_NAMES:
            raise ApiError(ErrCode.PARAM_INVALID, f"role 只能是 {list(ROLE_NAMES)}")
        user.role = role
    if "display_name" in body:
        user.display_name = str(body["display_name"])[:64]
    if "email" in body:
        user.email = str(body["email"])[:128]
    if "is_active" in body:
        if not isinstance(body["is_active"], bool):
            raise ApiError(ErrCode.PARAM_INVALID, "is_active 必须是布尔值")
        user.is_active = body["is_active"]
    if "password" in body and body["password"]:
        if len(str(body["password"])) < 6:
            raise ApiError(ErrCode.PARAM_INVALID, "口令至少 6 位")
        user.password_hash = hash_password(str(body["password"]))
    db.session.commit()
    write_audit("user_update", "sys_user", f"id={user_id} fields={','.join(sorted(body))}")
    return ok(user.to_dict())


@bp.delete("/users/<int:user_id>")
@require_role("admin")
def delete_user(user_id: int):
    identity = current_identity()
    user = db.session.query(User).filter_by(id=user_id).first()
    if user is None:
        raise ApiError(ErrCode.NOT_FOUND, "用户不存在")
    if user.username == identity["username"]:
        raise ApiError(ErrCode.PARAM_INVALID, "不能删除当前登录账号")
    db.session.delete(user)
    db.session.commit()
    write_audit("user_delete", "sys_user", f"{user.username}")
    return ok({"deleted": user_id})


# ---------------------------------------------------------------- 告警规则
@bp.get("/alerts/rules")
@require_role("ops")
def list_rules():
    rows = db.session.query(AlertRule).order_by(AlertRule.id.desc()).all()
    return ok({"items": [r.to_dict() for r in rows]})


@bp.post("/alerts/rules")
@require_role("admin")
def create_rule():
    body = params.json_body()
    rule = AlertRule(
        name=params.body_str(body, "name", required=True, max_len=128),
        metric=params.body_str(body, "metric", default="delay_rate", max_len=64),
        comparator=params.body_str(body, "comparator", default="gt", max_len=8),
        threshold=float(body.get("threshold", 0.3)),
        scope_type=params.body_str(body, "scope_type", default="airport", max_len=16),
        scope_value=params.body_str(body, "scope_value", default="*", max_len=128),
        level=params.body_str(body, "level", default="warning", max_len=16),
        created_by=current_identity()["username"],
    )
    db.session.add(rule)
    db.session.commit()
    write_audit("alert_rule_create", "sys_alert_rule", f"{rule.name} {rule.metric} "
                                                       f"{rule.comparator} {rule.threshold}")
    return ok(rule.to_dict())


@bp.delete("/alerts/rules/<int:rule_id>")
@require_role("admin")
def delete_rule(rule_id: int):
    rule = db.session.query(AlertRule).filter_by(id=rule_id).first()
    if rule is None:
        raise ApiError(ErrCode.NOT_FOUND, "规则不存在")
    db.session.delete(rule)
    db.session.commit()
    return ok({"deleted": rule_id})


@bp.get("/alerts/events")
@require_role("ops")
def list_events():
    period = params.get_str("period") or engine.BaseEngine.latest_period("ads_anomaly_alert")
    items = []
    if period:
        try:
            df = engine.read("ads_anomaly_alert", filters=[("year_month", "=", period)])
            items = engine.to_records(df.sort_values("observed", ascending=False).head(100))
        except ApiError:
            items = []
    rows = db.session.query(AlertEvent).order_by(AlertEvent.id.desc()).limit(50).all()
    return ok({"period": period, "warehouse_alerts": items,
               "manual_events": [r.to_dict() for r in rows]})


@bp.post("/alerts/events/<int:event_id>/ack")
@require_role("ops")
def ack_event(event_id: int):
    event = db.session.query(AlertEvent).filter_by(id=event_id).first()
    if event is None:
        raise ApiError(ErrCode.NOT_FOUND, "告警事件不存在")
    event.acked = True
    event.acked_by = current_identity()["username"]
    db.session.commit()
    return ok(event.to_dict())


@bp.post("/alerts/evaluate")
@require_role("admin")
def evaluate_alerts():
    """按规则扫描最新一期排行，命中的写 AlertEvent（演示"订阅延误率>30%"。"""
    period = params.get_str("period") or stats.latest_period()
    rules = db.session.query(AlertRule).filter_by(enabled=True).all()
    if not rules:
        return ok({"period": period, "triggered": 0, "note": "没有启用的告警规则"})
    rank_df = engine.read("ads_airport_rank", filters=[("year_month", "=", period)])
    triggered = []
    for rule in rules:
        subset = rank_df[rank_df["metric"] == rule.metric]
        if subset.empty:
            continue
        latest = subset[subset["stat_date"].astype(str) == subset["stat_date"].astype(str).max()]
        ops = {"gt": latest["value"] > rule.threshold, "ge": latest["value"] >= rule.threshold,
               "lt": latest["value"] < rule.threshold, "le": latest["value"] <= rule.threshold}
        hits = latest[ops.get(rule.comparator, latest["value"] > rule.threshold)]
        for row in hits.to_dict(orient="records"):
            event = AlertEvent(
                rule_id=rule.id, rule_name=rule.name,
                triggered_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                level=rule.level, scope=str(row.get("airport_iata")),
                observed=float(row.get("value") or 0),
                message=f"{rule.name}：{row.get('airport_iata')} {rule.metric}="
                        f"{row.get('value')} {rule.comparator} {rule.threshold}",
            )
            db.session.add(event)
            triggered.append(event.message)
    db.session.commit()
    return ok({"period": period, "triggered": len(triggered), "messages": triggered[:50]})
