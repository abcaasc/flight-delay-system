"""模型中心接口：版本、对比、泛化矩阵、训练、上线（P5/P6 + 功能模块 6/8）。"""
from __future__ import annotations

import json
import numpy as np
import pandas as pd
from flask import Blueprint
from app.config import BASE_DIR

from app.extensions import db
from app.models import ModelVersion
from app.services import engine, ingest, model as model_service
from app.services.cache import invalidate
from app.utils import params
from app.utils.auth import require_role, write_audit
from app.utils.response import ApiError, ErrCode, ok

bp = Blueprint("models", __name__, url_prefix="/api/v1/model")


@bp.get("/backtest")
@require_role("ops")
def backtest():
    path = BASE_DIR / "instance" / "rolling-backtest.json"
    if not path.is_file():
        raise ApiError(ErrCode.DATA_NOT_READY, "尚未生成时间回测报告")
    report = json.loads(path.read_text(encoding="utf-8"))
    active = db.session.query(ModelVersion.model_ver).filter_by(is_online=1, task="cls15").first()
    return ok({**report, "current": bool(active and active[0] == report["model_ver"])})


@bp.get("/versions")
@require_role("ops")
def versions():
    task = params.get_str("task")
    query = db.session.query(ModelVersion)
    if task:
        if task not in model_service.TASKS:
            raise ApiError(ErrCode.PARAM_INVALID, f"task 只能是 {list(model_service.TASKS)}")
        query = query.filter(ModelVersion.task == task)
    rows = query.order_by(ModelVersion.id.desc()).limit(params.get_int("limit", 50, minimum=1, maximum=200)).all()
    return ok({"items": [r.to_dict() for r in rows],
               "online": next((r.model_ver for r in rows if r.is_online), None),
               "tasks": {k: v["name"] for k, v in model_service.TASKS.items()}})


@bp.get("/compare")
@require_role("ops")
def compare():
    """多模型对比：同一任务不同版本的指标表 + 雷达图数据（P5）。"""
    task = params.get_str("task", "cls15", choices=list(model_service.TASKS))
    rows = (db.session.query(ModelVersion).filter_by(task=task)
            .order_by(ModelVersion.id.desc()).limit(10).all())
    if not rows:
        raise ApiError(ErrCode.DATA_NOT_READY, "尚无模型版本，请先训练（POST /api/v1/model/train）")
    items = []
    for row in rows:
        data = row.to_dict()
        metrics = data.get("metrics", {})
        test_metrics = metrics.get("test", {}) if isinstance(metrics, dict) else {}
        items.append({
            "model_ver": data["model_ver"], "algo": data["algo"], "task": data["task"],
            "train_range": data["train_range"], "test_range": data["test_range"],
            "is_online": data["is_online"], "created_at": data["created_at"],
            "metrics": test_metrics,
        })
    metric_keys = {"cls15": ["f1", "precision", "recall", "auc", "accuracy"],
                   "level3": ["accuracy", "macro_f1"],
                   "reg": ["mae", "rmse", "r2"]}[task]
    radar = [{
        "model_ver": item["model_ver"],
        "values": [{"metric": key, "value": item["metrics"].get(key)} for key in metric_keys],
    } for item in items]
    return ok({"task": task, "items": items, "radar": radar, "metrics": metric_keys})


@bp.get("/generalization")
@require_role("ops")
def generalization():
    """泛化矩阵：训练集覆盖的机场 × 测试期指标（跨机场泛化）。

    实现口径：用 ads_delay_pred 的预测值与真实标签，按机场分组算 F1/AUC，
    行=起飞机场，列=指标；再给一列"是否出现在训练集"，用于判断"训练没见过该机场"。
    """
    period = params.get_str("period") or engine.BaseEngine.latest_period("ads_delay_pred")
    if not period:
        raise ApiError(ErrCode.DATA_NOT_READY, "尚无预测结果，请先训练并批量预测")
    df = engine.read("ads_delay_pred", filters=[("year_month", "=", period)])
    if df.empty:
        raise ApiError(ErrCode.DATA_NOT_READY, f"{period} 无预测结果")
    df["actual"] = pd.to_numeric(df["actual_dep_del15"], errors="coerce")
    df["prob"] = pd.to_numeric(df["prob_delay"], errors="coerce")
    df = df.dropna(subset=["actual", "prob"])
    if df.empty:
        raise ApiError(ErrCode.EMPTY_RESULT, "预测结果缺少真实标签，无法评估")

    trained_airports = set()
    versions = df["model_ver"].dropna().unique() if "model_ver" in df else []
    payload = model_service.online_model(str(versions[0])) if len(versions) == 1 else None
    if payload:
        trained_airports = set(payload.get("trained_airports", []))
    threshold = float(payload.get("threshold", 0.5)) if payload else 0.5

    rows = []
    for iata, group in df.groupby("origin"):
        y = group["actual"].astype(int).to_numpy()
        p = group["prob"].to_numpy()
        metrics = model_service.classification_metrics(y, p, threshold)
        rows.append({
            "airport_iata": str(iata),
            "samples": int(len(group)),
            "f1": metrics["f1"], "auc": metrics["auc"],
            "accuracy": metrics["accuracy"],
            "actual_delay_rate": round(float(y.mean()), 4),
            "pred_delay_rate": round(float((p >= threshold).mean()), 4),
            "in_train_set": str(iata) in trained_airports if trained_airports else None,
        })
    rows.sort(key=lambda r: (r["auc"] or 0))
    return ok({
        "period": period,
        "matrix": rows,
        "worst": rows[0] if rows else None,
        "best": rows[-1] if rows else None,
        "threshold": threshold,
        "note": "按起飞机场分组评估；in_train_set 由该预测版本的训练机场集合判定。"
                "时间外推不等于机场留出实验，该页面不代表跨域验证结果。",
    })


@bp.post("/train")
@require_role("analyst")
def train():
    body = params.json_body(required=False)
    task = params.body_str(body, "task", default="cls15")
    if task not in model_service.TASKS:
        raise ApiError(ErrCode.PARAM_INVALID, f"task 只能是 {list(model_service.TASKS)}")
    algo = params.body_str(body, "algo", default="auto")
    if algo not in ("auto", "lightgbm", "baseline", "baseline_lr"):
        raise ApiError(ErrCode.PARAM_INVALID, "algo 只能是 auto/lightgbm/baseline/baseline_lr")
    periods = body.get("periods") or []
    if not isinstance(periods, list):
        raise ApiError(ErrCode.PARAM_INVALID, "periods 必须是数组，如 [\"2024-01\",\"2024-02\"]")
    from app.utils.timeutil import parse_ym
    try:
        periods = sorted({parse_ym(p) for p in periods})
    except (ValueError, TypeError):
        raise ApiError(ErrCode.PARAM_INVALID, "periods 必须由 yyyy-MM 月份组成")
    if not periods:
        periods = engine.BaseEngine.available_periods("dws_flight_features") or []
        if not periods:
            raise ApiError(ErrCode.DATA_NOT_READY, "特征宽表为空，请先执行 ETL")
        periods = periods[-3:]  # 默认最近 3 个月，够做时间切分

    job = ingest.create_job("train", "system", periods, mode="full",
                            operator=_operator(), params={"task": task, "algo": algo})
    write_audit("model_train", "model_version", f"task={task} algo={algo} periods={periods}")
    return ok(job)


@bp.post("/online")
@require_role("admin")
def set_online():
    body = params.json_body()
    model_ver = params.body_str(body, "model_ver", required=True, max_len=64)
    row = db.session.query(ModelVersion).filter_by(model_ver=model_ver).first()
    if row is None:
        raise ApiError(ErrCode.NOT_FOUND, f"模型版本不存在：{model_ver}")
    if row.task != "cls15" or not model_service.online_model(model_ver):
        raise ApiError(ErrCode.PARAM_INVALID, "在线延误概率预测需要有效的 cls15 模型产物")
    db.session.query(ModelVersion).update({ModelVersion.is_online: 0})
    row.is_online = 1
    db.session.commit()
    invalidate("pred")
    write_audit("model_online", "model_version", f"上线 {model_ver}")
    return ok({"model_ver": model_ver, "is_online": 1, "task": row.task, "algo": row.algo})


@bp.get("/feature-spec")
@require_role("ops")
def feature_spec():
    """特征白名单与基线权重（论文附录 / 泄漏检查用，A-8）。"""
    from app.config import Config
    from app.services.catalog import MODEL_FEATURES

    items = []
    for name, spec in model_service.FEATURE_SPECS.items():
        items.append({
            "feature": name, "name_cn": spec.name_cn, "center": spec.center,
            "scale": spec.scale, "coef": spec.coef,
            "in_whitelist": name in MODEL_FEATURES,
            "in_blacklist": name in Config.FEATURE_BLACKLIST,
        })
    return ok({
        "count": len(items),
        "items": items,
        "labels": list(model_service.TASKS.values()),
        "blacklist": list(Config.FEATURE_BLACKLIST),
        "engine": "lightgbm" if model_service._lightgbm_available() else "baseline_lr",
        "leakage_check": "0 个禁用字段" if all(not i["in_blacklist"] for i in items) else "存在禁用字段",
    })


def _operator() -> str:
    from app.utils.auth import current_identity

    return current_identity()["username"]

