"""回归验证统计覆盖、分页、模型版本与中断作业。"""
import json
import pickle
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import Config
from app.extensions import db
from app.models import JobLease, JobRun, ModelVersion
from app.services import engine, ingest, model


@pytest.fixture
def warehouse(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "WAREHOUSE_ROOT", tmp_path)
    rows = [
        ("a", "2024-01-01", "AA", 25, 1, 0),
        ("b", "2024-01-01", "AA", 25, 1, 0),
        ("c", "2024-01-02", "AA", -2, 0, 0),
        ("d", "2024-01-02", "DL", 38, 1, 0),
        ("e", "2024-01-02", "AA", None, None, 1),
        ("f", "2024-03-01", "AA", 65, 1, 0),
    ]
    detail = pd.DataFrame(rows, columns=["flight_sk", "flight_date", "reporting_airline",
                                         "dep_delay_min", "dep_del15", "cancelled"])
    detail["origin"] = "ORD"
    detail["dest"] = "LAX"
    detail["delay_level"] = [1, 1, 0, 1, -1, 2]
    detail["year_month"] = detail.flight_date.str[:7]
    for month, rows in detail.groupby("year_month"):
        engine.write("dwd_flight_detail", rows, partition_value=month, target_files=2)
    return tmp_path


def test_airline_scope_and_partial_coverage(client, warehouse):
    response = client.get("/api/v1/overview", query_string={
        "date_from": "2024-01-01", "date_to": "2024-03-31", "airlines": "AA", "airports": "ORD",
    }).get_json()["data"]
    kpi = response["kpi"]
    assert kpi["kpi"]["flights"] == 5
    assert kpi["kpi"]["delayed"] == 3
    assert kpi["kpi"]["cancelled"] == 1
    assert kpi["compared_to_previous"] == {}
    assert kpi["coverage"]["available_months"] == ["2024-01", "2024-03"]
    assert kpi["coverage"]["missing_months"] == ["2024-02"]
    assert sum(row["flights"] for row in response["trend"]["points"]) == 5


def test_flight_paging_filters_empty_coverage_and_sample(client, warehouse):
    filters = {"date_from": "2024-01-01", "date_to": "2024-03-31", "airline": "AA",
               "page_size": 2}
    def get(**extra):
        return client.get("/api/v1/flights", query_string={**filters, **extra}).get_json()

    for page, ids in [(1, ["f", "a"]), (2, ["b", "c"]), (3, ["e"]), (4, [])]:
        data = get(page=page)["data"]
        assert data["total"] == 5
        assert [row["flight_sk"] for row in data["items"]] == ids
    assert get(cancelled="true")["data"]["items"][0]["flight_sk"] == "e"
    assert get(cancelled="false", min_delay=1, max_delay=25)["data"]["total"] == 2
    assert get(dest="SFO")["data"]["total"] == 0
    absent = get(date_from="2024-02-01", date_to="2024-02-29")
    assert absent["code"] == 521
    assert absent["data"]["missing_months"] == ["2024-02"]
    (warehouse / "_sampled_flight_months.json").write_text('["2024-01"]', encoding="utf-8")
    assert get()["data"]["coverage"]["sampled_months"] == ["2024-01"]


def test_backtest_uses_loaded_model_version(client, auth, monkeypatch, tmp_path):
    report = tmp_path / "backtest.json"
    report.write_text(json.dumps({"model_ver": "effective"}), encoding="utf-8")
    monkeypatch.setattr(Config, "ROLLING_BACKTEST_REPORT", report)
    monkeypatch.setattr(model, "online_model", lambda: {"meta": {"model_ver": "effective"}})
    result = client.get("/api/v1/model/backtest", headers=auth("ops")).get_json()["data"]
    assert result["current"] is True


def test_model_artifact_resolves_after_moving_the_repository(app, monkeypatch, tmp_path):
    version = "baseline_qa_portable"
    monkeypatch.setattr(Config, "MODEL_ROOT", tmp_path)
    (tmp_path / f"{version}.pkl").write_bytes(pickle.dumps({"kind": "baseline", "task": "cls15"}))
    with app.app_context():
        row = ModelVersion(model_ver=version, task="cls15", artifact_path="old/path/model.pkl")
        db.session.add(row)
        db.session.commit()
        try:
            assert model.online_model(version)["meta"]["model_ver"] == version
        finally:
            db.session.delete(row)
            db.session.commit()


def test_form_thresholds_trade_recall_for_fewer_false_positives():
    y_valid = np.array([1, 1, 0, 0])
    p_valid = np.array([0.80, 0.19, 0.70, 0.15])
    points = model.form_operating_points(y_valid, p_valid, y_valid, p_valid)
    conservative = points["conservative"]
    assert conservative["valid"]["threshold"] == 0.80
    assert conservative["valid"]["recall"] >= points["validation_recall_floor"]
    assert conservative["test"]["confusion"]["fp"] == 0
    assert points["f1"]["test"]["confusion"]["fp"] == 1
    assert points["always_ontime"]["confusion"]["fn"] == 2
    different_test = model.form_operating_points(y_valid, p_valid, 1 - y_valid, p_valid[::-1])
    assert different_test["conservative"]["valid"] == conservative["valid"]


def test_form_model_masks_fallback_weather(monkeypatch):
    from app.services import predict_service

    monkeypatch.setattr(predict_service, "route_distance", lambda *_: 1200.0)
    rows = pd.DataFrame({"flight_date": ["2024-12-21"] * 2, "origin": ["ORD"] * 2,
                         "dest": ["SFO"] * 2, "dep_hour": [8, 8], "dep_slot": [16, 16],
                         "weather_available": [1, 0], "wind_max_ms": [12, 5]})
    features = model.form_feature_frame(rows, with_weather=True)
    assert list(features["weather_available"]) == [1, 0]
    assert features.loc[0, "wind_max_ms"] == 12
    assert features.loc[0, "vis_min_mi"] == model.FORM_WEATHER_DEFAULTS["vis_min_mi"]
    assert features.loc[1, model.FORM_WEATHER_FEATURES[1:]].isna().all()


def test_job_status_recovers_stale_leases_and_retries(client, auth, app, monkeypatch):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with app.app_context():
        for job_id in ("stale-qa", "fresh-qa", "legacy-qa"):
            db.session.add(JobRun(job_id=job_id, job_name="dims:system", status="running",
                                  started_at="2024-01-01 00:00:00"))
        args = {"job_type": "dims", "dataset": "system", "periods": [""],
                "mode": "incremental", "params": {}}
        db.session.add(JobLease(job_id="stale-qa", heartbeat_at=now - timedelta(minutes=15),
                                request_json=json.dumps(args)))
        db.session.add(JobLease(job_id="fresh-qa", heartbeat_at=now))
        db.session.commit()
    headers = auth("admin")
    def job(job_id):
        return client.get(f"/api/v1/ingest/jobs/{job_id}", headers=headers).get_json()["data"]
    assert job("fresh-qa")["status"] == "running"
    assert job("legacy-qa")["retryable"] is False
    assert job("stale-qa")["status"] == "interrupted"
    assert job("stale-qa")["retryable"] is True
    submitted = []
    monkeypatch.setattr(ingest, "create_job", lambda **kwargs: submitted.append(kwargs) or {"job_id": "retry-qa"})
    endpoint = "/api/v1/ingest/jobs/stale-qa/retry"
    assert client.post(endpoint, headers=auth("ops")).status_code == 403
    assert client.post(endpoint, headers=headers).get_json()["data"]["job_id"] == "retry-qa"
    assert submitted == [{**args, "operator": "admin"}]
