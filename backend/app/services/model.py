"""延误预测与归因服务。

两条预测路径，接口对外表现一致：
  1. LightGBM（装了 lightgbm 时）—— 真实训练，用 `pred_contrib=True` 拿精确 SHAP 值，
     因此不需要额外装 shap 包也能出单航班归因。
  2. 内置可解释基线（未装 lightgbm 时）—— 逻辑回归形式的加权模型，
     权重与标准化参数在 FEATURE_SPECS 中显式给出，归因 = w·z（线性模型的精确 Shapley 值）。

两条路径都输出：延误概率 / 二分类 / 三级分类 / 延误分钟 / 风险等级 / Top 归因因素，
对应开题报告功能模块（6）与设计说明书 FR-3 / FR-6。
"""
from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from app.config import Config
from app.extensions import db
from app.models import ModelVersion
from app.services import engine
from app.services.catalog import MODEL_FEATURES
from app.utils.logger import get_logger

log = get_logger(__name__)

TASKS = {
    "cls15": {"label": "dep_del15", "kind": "binary", "name": "延误二分类（>15min）"},
    "level3": {"label": "delay_level", "kind": "multiclass", "name": "三级延误分类"},
    "reg": {"label": "dep_delay_min", "kind": "regression", "name": "延误分钟回归"},
}


@dataclass(frozen=True)
class FeatureSpec:
    center: float
    scale: float
    coef: float          # 基线模型在 logit 空间的权重
    name_cn: str


# 标准化参数与基线权重（可在论文附录里整表给出，保证可复现）
FEATURE_SPECS: dict[str, FeatureSpec] = {
    "dep_hour": FeatureSpec(15, 5, 0.35, "计划起飞小时"),
    "dep_slot": FeatureSpec(30, 10, 0.05, "30 分钟时段序号"),
    "month": FeatureSpec(6.5, 3.0, 0.10, "月份"),
    "day_of_week": FeatureSpec(3, 2, 0.08, "星期几"),
    "is_weekend": FeatureSpec(0.30, 0.50, 0.10, "是否周末"),
    "is_holiday": FeatureSpec(0.05, 0.22, 0.15, "是否美国联邦假日"),
    "dow_sin": FeatureSpec(0, 0.70, 0.0, "星期正弦编码"),
    "dow_cos": FeatureSpec(0, 0.70, 0.0, "星期余弦编码"),
    "hour_sin": FeatureSpec(0, 0.70, 0.0, "小时正弦编码"),
    "hour_cos": FeatureSpec(0, 0.70, 0.0, "小时余弦编码"),
    "distance_mi": FeatureSpec(800, 600, -0.12, "航段距离（英里）"),
    "crs_elapsed_min": FeatureSpec(130, 60, -0.05, "计划飞行时长"),
    "origin_traffic_prev_day": FeatureSpec(400, 300, 0.20, "起飞机场前一日航班量"),
    "origin_delay_rate_prev_day": FeatureSpec(0.20, 0.10, 0.55, "起飞机场前一日延误率"),
    "origin_delay_rate_7d": FeatureSpec(0.20, 0.08, 0.45, "起飞机场近 7 日延误率"),
    "dest_delay_rate_7d": FeatureSpec(0.20, 0.08, 0.30, "到达机场近 7 日延误率"),
    "route_delay_rate_7d": FeatureSpec(0.20, 0.08, 0.50, "航线近 7 日延误率"),
    "airline_delay_rate_7d": FeatureSpec(0.20, 0.08, 0.35, "航司近 7 日延误率"),
    "chain_available": FeatureSpec(0.85, 0.35, 0.05, "能否重建航班链"),
    "prev_arr_delay_min": FeatureSpec(0, 20, 0.55, "前序航班到达延误"),
    "turnaround_min": FeatureSpec(90, 60, -0.10, "过站时间（分钟）"),
    "buffer_ratio": FeatureSpec(1.0, 0.40, -0.30, "过站缓冲比"),
    "risk_flag": FeatureSpec(0.20, 0.40, 0.40, "缓冲不足风险标记"),
    "weather_available": FeatureSpec(0.80, 0.40, 0.0, "气象特征是否可用"),
    "vis_min_mi": FeatureSpec(9, 3, -0.35, "起飞前窗口最低能见度（英里）"),
    "wind_max_ms": FeatureSpec(5, 3, 0.25, "起飞前窗口最大风速（m/s）"),
    "tmp_mean_c": FeatureSpec(15, 10, 0.0, "起飞前窗口均温（℃）"),
    "precip_mm": FeatureSpec(0.2, 1.5, 0.20, "起飞前窗口降水（mm）"),
}
BASELINE_BIAS = -1.15

# 校准参数文件：训练时把"基线截距"校准到训练集的实际延误率，并按验证集搜索最优判定阈值。
# 这样线上概率有实际含义（平均概率≈基准延误率），而不是拍脑袋的偏置。
CALIBRATION_FILE = Path(Config.MODEL_ROOT) / "calibration.json"
_CALIBRATION_CACHE: dict | None = None
_CALIBRATION_MTIME: float = 0.0

# 自动上线产物的缓存（key 为模型文件 mtime），避免每次预测都反序列化 1MB 的 pkl
_AUTOLOAD_CACHE: dict | None = None
_AUTOLOAD_MTIME: float = 0.0

# lightgbm 可用性探测结果（只探测一次）
_LGBM_STATE: dict[str, Any] = {}


def load_calibration() -> dict:
    """读取校准参数（mtime 变化时自动重载）；无文件时用默认值。"""
    global _CALIBRATION_CACHE, _CALIBRATION_MTIME
    try:
        mtime = CALIBRATION_FILE.stat().st_mtime
    except OSError:
        return {"bias": BASELINE_BIAS, "threshold": 0.5, "base_rate": None, "source": "default"}
    if _CALIBRATION_CACHE is not None and mtime == _CALIBRATION_MTIME:
        return _CALIBRATION_CACHE
    try:
        payload = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
        payload["source"] = "calibrated"
        _CALIBRATION_CACHE, _CALIBRATION_MTIME = payload, mtime
        return payload
    except Exception:
        return {"bias": BASELINE_BIAS, "threshold": 0.5, "base_rate": None, "source": "default"}


def save_calibration(bias: float, threshold: float, info: dict) -> dict:
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {"bias": round(float(bias), 6), "threshold": round(float(threshold), 4), **info}
    CALIBRATION_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    global _CALIBRATION_CACHE, _CALIBRATION_MTIME
    _CALIBRATION_CACHE, _CALIBRATION_MTIME = payload, CALIBRATION_FILE.stat().st_mtime
    log.info("基线校准已更新：bias=%.4f threshold=%.3f", bias, threshold)
    return payload


def decision_threshold() -> float:
    model = online_model()
    return float(model.get("threshold", 0.5) if model else load_calibration().get("threshold", 0.5))


# ============================================================ 通用指标
def auc_score(y_true: np.ndarray, y_score: np.ndarray) -> float | None:
    """rank-sum 形式实现 AUC（不依赖 sklearn）。"""
    y_true = np.asarray(y_true).astype(int)
    pos = int(y_true.sum())
    neg = int(len(y_true) - pos)
    if pos == 0 or neg == 0:
        return None
    ranks = pd.Series(y_score).rank().to_numpy()
    return float((ranks[y_true == 1].sum() - pos * (pos + 1) / 2) / (pos * neg))


def classification_metrics(y_true: np.ndarray, prob: np.ndarray, threshold: float = 0.5) -> dict:
    y_true = np.asarray(y_true).astype(int)
    pred = (np.asarray(prob) >= threshold).astype(int)
    tp = int(((pred == 1) & (y_true == 1)).sum())
    fp = int(((pred == 1) & (y_true == 0)).sum())
    fn = int(((pred == 0) & (y_true == 1)).sum())
    tn = int(((pred == 0) & (y_true == 0)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    accuracy = (tp + tn) / max(1, len(y_true))
    return {
        "f1": round(f1, 4), "precision": round(precision, 4), "recall": round(recall, 4),
        "accuracy": round(accuracy, 4),
        "auc": round(auc_score(y_true, prob), 4) if len(np.unique(y_true)) == 2 else None,
        "threshold": threshold, "support": int(len(y_true)),
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
    }


def regression_metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict:
    y_true = np.asarray(y_true, dtype="float64")
    y_pred = np.asarray(y_pred, dtype="float64")
    mask = ~(np.isnan(y_true) | np.isnan(y_pred))
    y_true, y_pred = y_true[mask], y_pred[mask]
    if len(y_true) == 0:
        return {"mae": None, "rmse": None, "r2": None, "support": 0}
    err = y_pred - y_true
    mae = float(np.abs(err).mean())
    rmse = float(math.sqrt((err ** 2).mean()))
    ss_res = float((err ** 2).sum())
    ss_tot = float(((y_true - y_true.mean()) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot else None
    return {"mae": round(mae, 3), "rmse": round(rmse, 3),
            "r2": round(r2, 4) if r2 is not None else None, "support": int(len(y_true))}


# ============================================================ 基线模型
def _feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """按 FEATURE_SPECS 取特征列并做列名对齐（缺失列补 NaN）。"""
    out = pd.DataFrame(index=df.index)
    for name in FEATURE_SPECS:
        source = name
        if name == "origin_traffic_prev_day" and "origin_traffic_24h" in df.columns:
            source = "origin_traffic_24h"
        out[name] = pd.to_numeric(df[source], errors="coerce") if source in df.columns else np.nan
    return out


def _zscore(features: pd.DataFrame) -> pd.DataFrame:
    z = pd.DataFrame(index=features.index)
    for name, spec in FEATURE_SPECS.items():
        col = features[name].astype("float64")
        # 缺失按"中性"处理：标准化后取 0（不引入未来信息，也不用全局均值）
        col = col.where(col.notna(), spec.center)
        z[name] = (col - spec.center) / spec.scale
    return z


def baseline_predict(features: pd.DataFrame, bias: float | None = None) -> tuple[np.ndarray, pd.DataFrame]:
    """返回 (延误概率, 每个特征的 logit 空间贡献)。"""
    z = _zscore(features)
    coefs = pd.Series({k: v.coef for k, v in FEATURE_SPECS.items()})
    bias = float(load_calibration().get("bias", BASELINE_BIAS) if bias is None else bias)
    logits = (z * coefs).sum(axis=1) + bias
    prob = 1 / (1 + np.exp(-logits.to_numpy()))
    contrib = z * coefs
    return prob, contrib


def calibrate_bias(z: pd.DataFrame, y: np.ndarray) -> float:
    """二分求截距，使平均预测概率 ≈ 训练集实际延误率（线性模型的标准截距校准）。"""
    coefs = pd.Series({k: v.coef for k, v in FEATURE_SPECS.items()})
    logit = (z * coefs).sum(axis=1).to_numpy()
    target = float(np.mean(y)) if len(y) else 0.15
    lo, hi = -9.0, 3.0
    for _ in range(80):
        mid = (lo + hi) / 2
        mean_prob = float(np.mean(1 / (1 + np.exp(-(logit + mid)))))
        if mean_prob > target:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def search_threshold(y_true: np.ndarray, prob: np.ndarray,
                     grid: tuple[float, ...] = tuple(np.round(np.arange(0.05, 0.76, 0.01), 2))) -> tuple[float, float]:
    """在验证集上网格搜索 F1 最优阈值（对应 §5.3：阈值不拍脑袋）。"""
    best_t, best_f1 = 0.5, -1.0
    for t in grid:
        f1 = classification_metrics(y_true, prob, float(t))["f1"]
        if f1 > best_f1:
            best_t, best_f1 = float(t), f1
    return best_t, best_f1


def _pred_minutes(prob: np.ndarray) -> np.ndarray:
    """概率 → 期望延误分钟。

    口径：E[延误] = P(延误) × E[延误|延误] − 提前起飞的偏置，
    其中 E[延误|延误] 取 58 分钟（BTS 延误航班的平均起飞延误量级），偏置 3 分钟。
    仅作排序与展示参考，回归任务的严格误差看 ads_model_metric 的 MAE/RMSE。
    """
    minutes = 58.0 * np.asarray(prob) - 3.0
    return np.clip(np.round(minutes, 1), -20, 300)


def _risk_level(prob: float, threshold: float | None = None) -> str:
    """风险等级按判定阈值分档：≥2×阈值=high，≥阈值=medium，其余=low。"""
    thr = float(threshold if threshold is not None else decision_threshold())
    if prob >= max(2 * thr, 0.35):
        return "high"
    if prob >= thr:
        return "medium"
    return "low"


def _level3_from_threshold(prob: np.ndarray, threshold: float | None = None) -> np.ndarray:
    """三级分类按判定阈值缩放：0 准点 / 1 轻微 / 2 严重。"""
    thr = float(threshold if threshold is not None else decision_threshold())
    p = np.asarray(prob)
    return np.where(p >= max(2 * thr, 0.35), 2, np.where(p >= thr, 1, 0))


def _level3(prob: np.ndarray) -> np.ndarray:
    """概率 → 三级分类：0 准点 / 1 轻微 / 2 严重。"""
    p = np.asarray(prob)
    return np.where(p >= 0.60, 2, np.where(p >= 0.25, 1, 0))


# ============================================================ 在线模型
def _lightgbm_available() -> bool:
    """探测 lightgbm 是否可用（只探测一次，失败原因打一条日志便于排查镜像缺库）。"""
    if "ok" in _LGBM_STATE:
        return bool(_LGBM_STATE["ok"])
    try:
        import lightgbm  # noqa: F401
        _LGBM_STATE["ok"] = True
    except Exception as exc:  # 常见：slim 镜像缺 libgomp.so.1
        _LGBM_STATE["ok"] = False
        log.warning("lightgbm 不可用，降级为内置基线模型：%s", exc)
    return bool(_LGBM_STATE["ok"])


def online_model(model_ver: str | None = None) -> dict | None:
    """读取当前上线模型；无则返回 None（走基线）。

    两步查找：
      1. 业务库 `model_version` 表的 is_online 记录（正式上线流程走这条）；
      2. 表里没有记录 / 产物文件丢失时，自动上线 MODEL_ROOT 下最新的
         `lightgbm_cls15_*.pkl`（公网演示容器每次重建都是全新 SQLite，
         没有这一步就只能跑降级基线，展示不出论文里训练的模型）。
    """
    query = db.session.query(ModelVersion)
    query = query.filter_by(model_ver=model_ver) if model_ver else query.filter_by(is_online=1, task="cls15")
    row = query.order_by(
        ModelVersion.id.desc()).first()
    if row is None:
        return None if model_ver else autoload_model()
    path = Path(row.artifact_path)
    if not row.artifact_path or not path.exists():
        log.warning("上线模型的产物文件不存在：%s", row.artifact_path)
        return None if model_ver else autoload_model()
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
        payload["meta"] = row.to_dict()
        return payload
    except Exception as exc:
        log.warning("加载模型 %s 失败：%s", row.model_ver, exc)
        return None if model_ver else autoload_model()


def _discover_artifact() -> Path | None:
    """在 MODEL_ROOT 下找最新的 LightGBM 二分类产物（找不到再退回任意 pkl）。"""
    root = Path(Config.MODEL_ROOT)
    if not root.is_dir():
        return None
    for pattern in ("lightgbm_cls15_*.pkl", "*.pkl"):
        candidates = [p for p in root.glob(pattern) if p.is_file()]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
    return None


def autoload_model() -> dict | None:
    """自动上线 MODEL_ROOT 下最新的 LightGBM 产物（带 mtime 缓存，只反序列化一次）。"""
    global _AUTOLOAD_CACHE, _AUTOLOAD_MTIME
    path = _discover_artifact()
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _AUTOLOAD_CACHE is not None and mtime == _AUTOLOAD_MTIME:
        return _AUTOLOAD_CACHE
    if not _lightgbm_available():
        return None
    try:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
    except Exception as exc:
        log.warning("自动上线模型 %s 失败：%s", path.name, exc)
        return None
    if payload.get("kind") != "lightgbm" or payload.get("task") != "cls15":
        return None
    payload["model_ver"] = payload.get("model_ver") or path.stem
    payload["meta"] = {"model_ver": payload["model_ver"], "artifact_path": str(path),
                       "source": "autoload", "is_online": 1, "task": "cls15"}
    _AUTOLOAD_CACHE, _AUTOLOAD_MTIME = payload, mtime
    log.info("数据库无上线记录，自动上线模型产物：%s", path.name)
    return payload


def predict_frame(features: pd.DataFrame, model_ver: str | None = None) -> dict:
    """统一预测入口：优先上线模型，否则基线。

    入参可以是原始特征行（含 origin_traffic_24h 之类的别名列），这里统一做一次
    `_feature_frame` 归一，保证送给模型的列集合与训练时完全一致。
    """
    features = _feature_frame(features)
    model = online_model(model_ver)
    if model_ver and (not model or model.get("task") != "cls15"):
        raise ValueError(f"二分类模型产物不可用：{model_ver}")
    if model and model.get("kind") == "lightgbm" and model.get("task") == "cls15":
        learner = model["model"]
        X = features[[m for m in FEATURE_SPECS if m in features.columns]]
        # LGBMClassifier.predict 返回"类别标签"，概率必须走 predict_proba
        if hasattr(learner, "predict_proba"):
            prob = learner.predict_proba(X)[:, 1]
        else:
            prob = learner.predict(X)
        try:
            raw = learner.predict(X, pred_contrib=True)
        except TypeError:
            raw = learner.booster_.predict(X, pred_contrib=True)
        contrib = pd.DataFrame(np.asarray(raw)[:, :-1], columns=X.columns, index=features.index)
        return {"prob": np.asarray(prob), "contrib": contrib, "engine": "lightgbm",
                "base_logit": np.asarray(raw)[:, -1], "threshold": float(model.get("threshold", 0.5)),
                "model_ver": model["meta"]["model_ver"]}
    bias = float(model.get("bias", BASELINE_BIAS) if model else load_calibration().get("bias", BASELINE_BIAS))
    prob, contrib = baseline_predict(features, bias=bias)
    ver = model["meta"]["model_ver"] if model else baseline_version()
    threshold = float(model.get("threshold", 0.5) if model else load_calibration().get("threshold", 0.5))
    return {"prob": prob, "contrib": contrib, "engine": "baseline", "model_ver": ver,
            "base_logit": np.full(len(features), bias), "threshold": threshold}


def baseline_version() -> str:
    return f"baseline_lr_v1"


def top_factors(contrib_row: pd.Series, prob: float, k: int = 10) -> list[dict]:
    """把 logit 贡献转成人类可读的归因列表（含影响百分点）。"""
    scale = max(prob * (1 - prob), 1e-6)
    items = []
    for name, value in contrib_row.items():
        if pd.isna(value):
            continue
        spec = FEATURE_SPECS[name]
        items.append({
            "feature": name,
            "name_cn": spec.name_cn,
            "contribution_logit": round(float(value), 4),
            "impact_pp": round(float(value) * scale * 100, 2),
            "direction": "increase" if value > 0 else "decrease",
        })
    items.sort(key=lambda x: abs(x["contribution_logit"]), reverse=True)
    return items[:k]


# ============================================================ 特征上下文（供单航班预测）
def feature_context(period: str | None = None, as_of: str | None = None) -> dict:
    """从最近一期特征宽表取机场/航线/航司的滞后延误率，供在线单航班预测使用。"""
    if as_of is not None:
        day = pd.Timestamp(as_of).normalize()
        start = day - pd.Timedelta(days=7)
        empty = {"period": None, "origin": {}, "origin_prev": {}, "dest": {},
                 "route": {}, "airline": {}, "traffic": {}}
        periods = sorted({start.strftime("%Y-%m"), (day - pd.Timedelta(days=1)).strftime("%Y-%m")})
        frames = []
        available = set(engine.BaseEngine.available_periods("dwd_flight_detail"))
        for month in periods:
            if month in available:
                frames.append(engine.read("dwd_flight_detail",
                    columns=["flight_date", "origin", "dest", "route_key", "reporting_airline", "dep_del15", "cancelled"],
                    filters=[("year_month", "=", month), ("flight_date", ">=", start.strftime("%Y-%m-%d")),
                             ("flight_date", "<", day.strftime("%Y-%m-%d"))]))
        if not frames:
            return empty
        history = pd.concat(frames, ignore_index=True)
        dates = pd.to_datetime(history["flight_date"], errors="coerce")
        history = history[(dates >= start) & (dates < day)].copy()
        history["dep_del15"] = pd.to_numeric(history["dep_del15"], errors="coerce")
        history = history[(pd.to_numeric(history["cancelled"], errors="coerce") != 1)
                          & history["dep_del15"].isin([0, 1])]
        if history.empty:
            return empty
        previous = history[history["flight_date"].astype(str) == (day - pd.Timedelta(days=1)).strftime("%Y-%m-%d")]
        return {"period": ",".join(periods),
                "origin": history.groupby("origin")["dep_del15"].mean().to_dict(),
                "origin_prev": previous.groupby("origin")["dep_del15"].mean().to_dict(),
                "dest": history.groupby("dest")["dep_del15"].mean().to_dict(),
                "route": history.groupby("route_key")["dep_del15"].mean().to_dict(),
                "airline": history.groupby("reporting_airline")["dep_del15"].mean().to_dict(),
                "traffic": previous.groupby("origin").size().to_dict()}
    period = period or engine.BaseEngine.latest_period("dws_flight_features")
    if not period:
        return {"period": None, "origin": {}, "dest": {}, "route": {}, "airline": {}, "traffic": {}}
    df = engine.read("dws_flight_features",
                     columns=["origin", "dest", "route_key", "reporting_airline", "flight_date",
                              "origin_delay_rate_7d", "dest_delay_rate_7d", "route_delay_rate_7d",
                              "airline_delay_rate_7d", "origin_traffic_24h"],
                     filters=[("year_month", "=", period)])
    if df.empty:
        return {"period": period, "origin": {}, "dest": {}, "route": {}, "airline": {}, "traffic": {}}

    def _mean_by(key: str, value: str) -> dict:
        return (df.groupby(key)[value].mean().round(4).to_dict())

    return {
        "period": period,
        "origin": _mean_by("origin", "origin_delay_rate_7d"),
        "dest": _mean_by("dest", "dest_delay_rate_7d"),
        "route": _mean_by("route_key", "route_delay_rate_7d"),
        "airline": _mean_by("reporting_airline", "airline_delay_rate_7d"),
        "traffic": _mean_by("origin", "origin_traffic_24h"),
    }


# ============================================================ 训练与评测
def load_training_frame(periods: list[str]) -> pd.DataFrame:
    frames = []
    # 训练只读白名单数值特征与切分/标签列；不加载 SHAP 文本等无关宽字段。
    columns = list(dict.fromkeys(["flight_date", "origin", "dep_del15", "delay_level",
                                 "dep_delay_min", *MODEL_FEATURES]))
    for period in periods:
        part = engine.read("dws_flight_features", columns=columns,
                           filters=[("year_month", "=", period)])
        if not part.empty:
            frames.append(part)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    # R-04：取消航班不参与分类训练
    if "delay_level" in df.columns:
        df = df[df["delay_level"] != -1]
    return df


def _time_split(df: pd.DataFrame, ratio: tuple[float, float, float]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """按时间顺序切分（禁止随机切分，§数据源.md 注意事项 4）。"""
    df = df.copy()
    dates = pd.to_datetime(df["flight_date"], errors="coerce")
    df = df[dates.notna()].sort_values(["flight_date", "dep_hour"], kind="mergesort")
    counts = df.groupby("flight_date", sort=True).size()
    if len(counts) < 3:
        raise ValueError("时间外推评估至少需要 3 个不同日期，保证训练/验证/测试日期互不重叠")
    cumulative = counts.cumsum().to_numpy()
    first = min(range(len(counts) - 2), key=lambda i: abs(cumulative[i] - len(df) * ratio[0]))
    second = min(range(first + 1, len(counts) - 1),
                 key=lambda i: abs(cumulative[i] - len(df) * (ratio[0] + ratio[1])))
    return (df.iloc[:cumulative[first]], df.iloc[cumulative[first]:cumulative[second]],
            df.iloc[cumulative[second]:])


def train_and_evaluate(periods: list[str], task: str = "cls15", algo: str = "auto",
                       operator: str = "system", params: dict | None = None) -> dict:
    """训练 + 评测 + 写 ADS 指标与版本记录。返回训练报告。"""
    if task not in TASKS:
        raise ValueError(f"未知任务：{task}")
    df = load_training_frame(periods)
    if df.empty:
        raise ValueError("训练数据为空，请先跑 dws_flight_features")

    if algo not in ("auto", "lightgbm", "baseline", "baseline_lr"):
        raise ValueError("algo 只能是 auto/lightgbm/baseline/baseline_lr")
    if algo == "lightgbm" and (not _lightgbm_available() or task != "cls15"):
        raise ValueError("当前 LightGBM 训练仅支持 cls15，且必须安装 lightgbm")
    label = TASKS[task]["label"]
    df = df.copy()
    df[label] = pd.to_numeric(df[label], errors="coerce")
    df = df[np.isfinite(df[label])]
    if task == "cls15":
        df = df[df[label].isin([0, 1])]
    elif task == "level3":
        df = df[df[label].isin([0, 1, 2])]
    ratio = Config.TRAIN_VALID_TEST_RATIO
    train, valid, test = _time_split(df, ratio)
    label = TASKS[task]["label"]
    features = [f for f in FEATURE_SPECS if f in df.columns]
    X_train = _feature_frame(train)[list(FEATURE_SPECS)]
    X_test = _feature_frame(test)[list(FEATURE_SPECS)]

    use_lgbm = algo in ("auto", "lightgbm") and _lightgbm_available() and task == "cls15"
    if task == "cls15" and train[label].nunique() < 2:
        raise ValueError("二分类训练集必须同时包含准点和延误航班")
    params = params or {}
    seed = Config.RANDOM_SEED
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    algo_name = "lightgbm" if use_lgbm else "baseline_lr"
    model_ver = f"{algo_name}_{task}_{stamp}"
    report: dict[str, Any] = {
        "model_ver": model_ver, "task": task, "algo": algo_name,
        "train_rows": int(len(train)), "valid_rows": int(len(valid)), "test_rows": int(len(test)),
        "train_range": f"{train['flight_date'].min()}~{train['flight_date'].max()}",
        "test_range": f"{test['flight_date'].min()}~{test['flight_date'].max()}",
        "features": len(FEATURE_SPECS), "seed": seed,
        "splits": {},
    }

    artifact_path = ""
    if TASKS[task]["kind"] == "regression":
        y_train = train[label].astype("float64").fillna(0).to_numpy()
        y_test = test[label].astype("float64").to_numpy()
        # 回归基线：用概率映射后的分钟数
        prob_train, _ = baseline_predict(_feature_frame(train))
        prob_test, _ = baseline_predict(_feature_frame(test))
        pred_train = _pred_minutes(prob_train)
        pred_test = _pred_minutes(prob_test)
        # 简单的最小二乘缩放（一维），让基线不至于系统性偏移
        denom = float((pred_train ** 2).sum()) or 1.0
        scale = float((pred_train * y_train).sum() / denom)
        report["calibration_scale"] = round(scale, 4)
        report["splits"]["train"] = regression_metrics(y_train, pred_train * scale)
        report["splits"]["test"] = regression_metrics(y_test, pred_test * scale)
        report["splits"]["valid"] = regression_metrics(
            valid[label].astype("float64").fillna(0).to_numpy(),
            _pred_minutes(baseline_predict(_feature_frame(valid))[0]) * scale)
        payload = {"kind": "baseline", "task": task, "scale": scale}
    else:
        y_train = train[label].astype("float64").fillna(0).astype(int).to_numpy()
        y_test = test[label].astype("float64").fillna(0).astype(int).to_numpy()
        y_valid = valid[label].astype("float64").fillna(0).astype(int).to_numpy()

        if TASKS[task]["kind"] == "binary":
            base_rate = float(np.mean(y_train)) if len(y_train) else 0.15
            if use_lgbm:
                import lightgbm as lgb

                # 参数按"3 万行 / 28 特征"的规模调过：叶子数与最小叶样本都要克制，
                # 否则概率会退化成 0/1 两档（AUC 好看但概率没有含义、SHAP 也不稳定）。
                booster = lgb.LGBMClassifier(
                    n_estimators=int(params.get("n_estimators", 250)),
                    learning_rate=float(params.get("learning_rate", 0.03)),
                    num_leaves=int(params.get("num_leaves", 15)),
                    min_child_samples=int(params.get("min_child_samples", 120)),
                    min_split_gain=float(params.get("min_split_gain", 0.0)),
                    subsample=float(params.get("subsample", 0.8)), subsample_freq=1,
                    colsample_bytree=float(params.get("colsample_bytree", 0.8)),
                    reg_lambda=float(params.get("reg_lambda", 5.0)),
                    random_state=seed, n_jobs=4, verbose=-1,
                )
                booster.fit(X_train, y_train)
                prob_test = booster.predict_proba(X_test)[:, 1]
                prob_valid = booster.predict_proba(_feature_frame(valid)[list(FEATURE_SPECS)])[:, 1]
                prob_train = booster.predict_proba(X_train)[:, 1]
                payload = {"kind": "lightgbm", "task": "cls15", "model": booster,
                           "features": list(FEATURE_SPECS), "model_ver": model_ver}
                bias = float(load_calibration().get("bias", BASELINE_BIAS))
            else:
                # 1) 截距校准：让平均预测概率 ≈ 训练集实际延误率
                bias = calibrate_bias(_zscore(X_train), y_train)
                prob_test, _ = baseline_predict(X_test, bias=bias)
                prob_valid, _ = baseline_predict(_feature_frame(valid), bias=bias)
                prob_train, _ = baseline_predict(X_train, bias=bias)
                payload = {"kind": "baseline", "task": task, "bias": bias}

            # 2) 判定阈值在验证集上网格搜索（F1 最优），避免固定 0.5 在低基准率下失衡
            threshold, best_f1 = search_threshold(y_valid, prob_valid)
            payload.update(bias=bias, threshold=threshold)
            report["calibration"] = {"bias": round(bias, 4), "threshold": threshold,
                                     "base_rate": round(base_rate, 4),
                                     "valid_best_f1": round(best_f1, 4)}
            report["splits"]["test"] = classification_metrics(y_test, prob_test, threshold)
            report["splits"]["valid"] = classification_metrics(y_valid, prob_valid, threshold)
            report["splits"]["train"] = classification_metrics(y_train, prob_train, threshold)
        else:  # 三级分类：基线下用概率分段
            payload = {"kind": "baseline", "task": task}
            prob_test, _ = baseline_predict(X_test)
            prob_valid, _ = baseline_predict(_feature_frame(valid))
            pred_test = _level3(prob_test)
            pred_valid = _level3(prob_valid)
            report["splits"]["test"] = {
                "accuracy": round(float((pred_test == y_test).mean()), 4),
                "macro_f1": round(float(np.mean([
                    classification_metrics((y_test == c).astype(int),
                                           (pred_test == c).astype(int))["f1"]
                    for c in (0, 1, 2)])), 4),
                "support": int(len(y_test)),
            }
            report["splits"]["valid"] = {
                "accuracy": round(float((pred_valid == y_valid).mean()), 4),
                "support": int(len(y_valid)),
            }

    # 每个版本独立保存参数；训练候选版本不会改变当前线上模型。
    payload["trained_airports"] = sorted(train["origin"].dropna().unique().tolist()) if "origin" in train else []
    payload["model_ver"] = model_ver
    artifact_path = str(Path(Config.MODEL_ROOT) / f"{model_ver}.pkl")
    Path(Config.MODEL_ROOT).mkdir(parents=True, exist_ok=True)
    with open(artifact_path, "wb") as fh:
        pickle.dump(payload, fh)

    # ---------------- 写模型版本与指标 ----------------
    version = ModelVersion(
        model_ver=model_ver, task=task, algo=algo_name,
        feature_set=",".join(FEATURE_SPECS), train_range=report["train_range"],
        test_range=report["test_range"],
        params_json=json.dumps({**params, "seed": seed}, ensure_ascii=False),
        metrics_json=json.dumps(report["splits"], ensure_ascii=False),
        artifact_path=artifact_path, is_online=0, seed=seed, created_by=operator,
        created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.session.add(version)
    db.session.commit()

    # ---------------- 写 ads_model_metric ----------------
    rows = []
    for split, metrics in report["splits"].items():
        rows.append({
            "model_ver": model_ver, "task": task, "split": split, "algo": algo_name,
            "f1": metrics.get("f1"), "precision": metrics.get("precision"),
            "recall": metrics.get("recall"), "auc": metrics.get("auc"),
            "accuracy": metrics.get("accuracy"), "mae": metrics.get("mae"),
            "rmse": metrics.get("rmse"), "r2": metrics.get("r2"),
            "threshold": metrics.get("threshold", 0.5),
            "support": metrics.get("support"), "created_at": version.created_at,
        })
    try:
        engine.write("ads_model_metric", pd.DataFrame(rows), mode="append", target_files=1)
    except Exception as exc:
        log.warning("写 ads_model_metric 失败：%s", exc)

    report["artifact_path"] = artifact_path
    log.info("训练完成：%s，测试集指标 %s", model_ver, report["splits"].get("test"))
    return report


def predict_and_store(period: str, model_ver: str | None = None,
                      operator: str = "system", top_k: int = 5) -> dict:
    """对某月全部航班批量预测并写 ads_delay_pred（P7 归因页与高风险高亮的来源）。"""
    df = engine.read("dws_flight_features", filters=[("year_month", "=", period)])
    if df.empty:
        raise ValueError(f"{period} 无特征宽表数据")
    result = predict_frame(_feature_frame(df), model_ver=model_ver)
    prob = result["prob"]
    contrib = result["contrib"]

    out = pd.DataFrame({
        "flight_sk": df["flight_sk"].astype(str),
        "model_ver": result["model_ver"],
        "year_month": period,
        "flight_date": df["flight_date"].astype(str),
        "origin": df["origin"], "dest": df["dest"],
        "reporting_airline": df["reporting_airline"],
        "dep_hour": pd.to_numeric(df["dep_hour"], errors="coerce"),
        "prob_delay": np.round(prob, 4),
        "pred_class": (prob >= result["threshold"]).astype(int),
        "pred_level": _level3_from_threshold(prob, result["threshold"]),
        "pred_minutes": _pred_minutes(prob),
        "risk_level": [_risk_level(p, result["threshold"]) for p in prob],
        "actual_dep_delay_min": pd.to_numeric(df["dep_delay_min"], errors="coerce"),
        "actual_dep_del15": pd.to_numeric(df["dep_del15"], errors="coerce"),
    })
    factors = []
    for i in range(len(out)):
        items = top_factors(contrib.iloc[i], float(prob[i]), k=top_k)
        factors.append(json.dumps([f["feature"] for f in items], ensure_ascii=False))
    out["top_factors"] = factors

    written = engine.write("ads_delay_pred", out, partition_value=period, mode="overwrite",
                           target_files=4)
    log.info("批量预测完成：%s，%d 行，引擎 %s", period, written["rows"], result["engine"])
    return {"period": period, "rows": written["rows"], "engine": result["engine"],
            "model_ver": out["model_ver"].iloc[0],
            "high_risk": int((out["risk_level"] == "high").sum()),
            "avg_prob": round(float(np.mean(prob)), 4)}


