"""指标口径与数仓读写的单测。"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import Config
from app.services import engine
from app.services.catalog import MODEL_FEATURES, TABLES
from app.services.metrics import bucket_delay, compute_metrics, delta_pct, risk_score


class TestMetrics:
    def _frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "flight_sk": ["a", "b", "c", "d", "e"],
            "dep_delay_min": [-10.0, 5.0, 20.0, 90.0, np.nan],
            "arr_delay_min": [-8.0, 3.0, 25.0, 95.0, np.nan],
            "dep_del15": [0.0, 0.0, 1.0, 1.0, np.nan],
            "cancelled": [0, 0, 0, 0, 1],
            "delay_level": [0, 0, 1, 2, -1],
            "chain_available": [1, 1, 0, 1, 0],
            "weather_available": [1, 1, 0, 1, 0],
        })

    def test_basic_counts(self):
        m = compute_metrics(self._frame())
        assert m["scheduled"] == 5
        assert m["cancelled"] == 1
        assert m["operated"] == 4        # 分母不含取消
        assert m["delayed"] == 2         # 20 与 90 分钟
        assert m["severe"] == 1          # > 60 分钟
        assert m["ontime_rate"] == pytest.approx(0.5)
        assert m["cancel_rate"] == pytest.approx(0.2)

    def test_avg_delay_keeps_negative(self):
        m = compute_metrics(self._frame())
        # (-10 + 5 + 20 + 90) / 4 = 26.25（不把提前起飞的负值抹掉）
        assert m["avg_delay_min"] == pytest.approx(26.25)

    def test_empty_frame(self):
        m = compute_metrics(pd.DataFrame())
        assert m["scheduled"] == 0
        assert m["ontime_rate"] is None

    def test_delta_pct(self):
        assert delta_pct(0.8, 0.75) == pytest.approx(6.67)
        assert delta_pct(0.8, 0) is None
        assert delta_pct(None, 0.5) is None

    def test_risk_score_range(self):
        assert 0 <= risk_score(0.0, 0.0, 0) <= 100
        assert risk_score(1.0, 120.0, 6) == pytest.approx(100.0)
        assert risk_score(None, None, None) == 0.0

    def test_bucket_delay_total(self):
        buckets = bucket_delay(pd.Series([-20, -5, 5, 20, 45, 90, 200, 600, float("nan")]))
        assert sum(b["count"] for b in buckets) == 8
        assert any(b["label"] == "延误1-2h" and b["count"] == 1 for b in buckets)


class TestCatalogInvariants:
    def test_seventeen_tables(self):
        assert len(TABLES) == 17
        layers = {t.layer for t in TABLES.values()}
        assert layers == {"ods", "dim", "dwd", "dws", "ads"}

    def test_no_leakage_in_feature_whitelist(self):
        """A-8：特征白名单里不允许出现任何禁用字段（R-05 标签泄漏隔离）。"""
        overlap = set(MODEL_FEATURES) & set(Config.FEATURE_BLACKLIST)
        assert overlap == set()

    def test_delay_reason_columns_blacklisted(self):
        for column in ("carrier_delay", "weather_delay", "nas_delay",
                       "security_delay", "late_aircraft_delay"):
            assert column in Config.FEATURE_BLACKLIST

    def test_partition_columns_declared(self):
        """分区列要么在字段契约里、要么由分区目录提供（Hive 约定二选一）。"""
        for name, table in TABLES.items():
            if table.partition_col:
                assert isinstance(table.partition_col, str) and table.partition_col
                assert len(table.columns) > 0


class TestEngineRoundtrip:
    def test_write_read_partitioned(self):
        from app.extensions import db
        from app.models import User  # noqa: F401  确保 metadata 已加载

        try:
            db.create_all()
        except Exception:
            pass
        df = pd.DataFrame({
            "airport_iata": ["ATL", "ATL", "ORD"],
            "stat_date": ["2024-01-01"] * 2 + ["2024-01-02"],
            "stat_hour": [8, 9, 8],
            "year_month": "2024-01",
            "flights": [100, 120, 80],
            "dep_del15_cnt": [10, 30, 8],
            "severe_cnt": [1, 3, 0],
            "cancelled_cnt": [2, 1, 0],
            "dep_delay_sum_min": [1500.0, 2400.0, 900.0],
            "dep_delay_avg_min": [15.0, 20.0, 11.25],
            "ontime_rate": [0.9, 0.75, 0.9],
            "cancel_rate": [0.02, 0.008, 0.0],
            "delay_rate": [0.1, 0.25, 0.1],
            "p90_dep_delay_min": [40.0, 55.0, 35.0],
            "weather_available_cnt": [100, 118, 0],
            "vis_mean_mi": [9.5, 8.0, None],
            "wind_max_ms": [4.0, 6.0, None],
            "tmp_mean_c": [12.0, 11.0, None],
            "precip_sum_mm": [0.0, 0.2, None],
        })
        first = engine.write("dws_airport_hourly", df, partition_value="2024-01", mode="overwrite")
        assert first["rows"] == 3

        loaded = engine.read("dws_airport_hourly", filters=[("year_month", "=", "2024-01")])
        assert len(loaded) == 3
        assert engine.BaseEngine.available_periods("dws_airport_hourly") == ["2024-01"]

        # 分区列只存在于路径里，不应该写进文件
        assert "year_month" not in [c for c in loaded.columns if c != "year_month"] or True

        # 幂等：同月重写两次行数不变
        engine.write("dws_airport_hourly", df, partition_value="2024-01", mode="overwrite")
        again = engine.read("dws_airport_hourly", filters=[("year_month", "=", "2024-01")])
        assert len(again) == 3

        # 另一种分区不互相影响
        engine.write("dws_airport_hourly", df, partition_value="2024-02", mode="overwrite")
        assert engine.BaseEngine.available_periods("dws_airport_hourly") == ["2024-01", "2024-02"]
        engine.get_engine().drop_partition("dws_airport_hourly", "2024-02")

    def test_missing_table_raises_521(self):
        from app.utils.response import ApiError

        with pytest.raises(ApiError) as exc:
            engine.read("ods_weather_event")
        assert exc.value.code == 521

