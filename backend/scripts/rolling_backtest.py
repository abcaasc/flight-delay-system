"""按月前移训练、校准和测试窗口，输出只读验证报告。运行 python scripts/rolling_backtest.py。"""
import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.base import clone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from app.config import BASE_DIR
from app.services import model


def samples(period, date_range=None):
    frame = model.load_training_frame([str(period)])
    if frame.empty:
        raise ValueError(f"{period} 缺少特征分区")
    subset = frame["dep_del15"].isin([0, 1])
    if date_range:
        subset &= frame["flight_date"].astype(str).between(*date_range)
    rows = frame.loc[subset]
    if rows.empty:
        raise ValueError(f"{period} 缺少有效标签")
    features = model._feature_frame(rows)
    # 留出集保留上线模型训练时的数值精度，确保能够复现登记的测试指标。
    if date_range is None:
        features = features.astype("float32")
    return features, rows["dep_del15"].to_numpy(dtype="int8")


def evaluate(learner, features, labels, threshold):
    full = model.classification_metrics(labels, learner.predict_proba(features)[:, 1], threshold)
    # 与 predict_service.build_feature_row 的在线缺链输入一致。仅隔离航班链的影响。
    masked = features.assign(chain_available=0, prev_arr_delay_min=np.nan,
                             turnaround_min=np.nan, buffer_ratio=np.nan, risk_flag=0)
    no_chain = model.classification_metrics(labels, learner.predict_proba(masked)[:, 1], threshold)
    return {"full": full, "no_chain": no_chain}


def fold(test, prototype):
    train_months = [test - 3, test - 2]
    train = [samples(p) for p in train_months]
    learner = clone(prototype)
    train_rows = sum(len(y) for _, y in train)
    learner.fit(pd.concat([x for x, _ in train], ignore_index=True),
                np.concatenate([y for _, y in train]))
    del train
    valid_x, valid_y = samples(test - 1)
    threshold, _ = model.search_threshold(valid_y, learner.predict_proba(valid_x)[:, 1])
    valid_rows = len(valid_y)
    del valid_x, valid_y
    x, y = samples(test)
    return {"train": list(map(str, train_months)), "train_rows": train_rows,
            "validation": str(test - 1), "validation_rows": valid_rows, "test": str(test),
            **evaluate(learner, x, y, threshold)}


def run(test_periods, output):
    online = model.online_model()
    if not online or online["kind"] != "lightgbm" or online["task"] != "cls15":
        raise ValueError("需要已上线的 LightGBM 二分类模型")
    meta = online["meta"]
    test_range = meta["test_range"].split("~")
    if len(test_range) != 2 or not meta["train_range"].split("~")[-1] < test_range[0] <= test_range[1]:
        raise ValueError("上线模型缺少独立的时间测试集")
    test_months = pd.period_range(*test_range, freq="M")
    heldout = [samples(p, test_range) for p in test_months]
    paired = evaluate(online["model"], pd.concat([x for x, _ in heldout]),
                      np.concatenate([y for _, y in heldout]), online["threshold"])
    del heldout
    folds = []
    for month in test_periods:
        print(f"测试窗口 {month}", flush=True)
        folds.append(fold(month, online["model"]))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "model_ver": meta["model_ver"],
              "holdout": {"test_range": test_range, **paired}, "folds": folds}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as f:
        json.dump(report, f, ensure_ascii=False, indent=2, allow_nan=False)
    os.replace(f.name, output)
    print(output, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-months", nargs="+", default=[
        "2015-04", "2018-07", "2020-10", "2022-01", "2024-03", "2024-06", "2024-09", "2024-12"])
    args = parser.parse_args()
    with create_app().app_context():
        run(sorted(set(pd.Period(p, freq="M") for p in args.test_months)),
            BASE_DIR / "instance" / "rolling-backtest.json")
