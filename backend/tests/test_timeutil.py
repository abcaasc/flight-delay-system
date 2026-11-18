"""清洗规则 R-02 / R-01 与时间口径的单测（§八 测试规范：每条规则 1 正 1 反例）。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.etl.common import to_clock_minutes, to_num
from app.utils.timeutil import hhmm_to_minutes, iter_months, minutes_to_hhmm, parse_ym, prev_month


class TestHHMM:
    @pytest.mark.parametrize("raw,expected", [
        ("0900", 540), ("0000", 0), ("2359", 1439), ("9", 9), ("1", 1),
        ("2400", 1440), (900, 540), ("0855", 535),
    ])
    def test_valid(self, raw, expected):
        value, status = hhmm_to_minutes(raw)
        assert value == expected
        assert status in ("ok", "fixed_2400")

    @pytest.mark.parametrize("raw", ["", None, "9999", "1260", "2405", "12:00", "abcd", "10000", float("nan")])
    def test_invalid_or_empty(self, raw):
        value, status = hhmm_to_minutes(raw)
        assert value is None
        assert status in ("empty", "invalid")

    def test_roundtrip(self):
        assert minutes_to_hhmm(540) == "0900"
        assert minutes_to_hhmm(1440) == "0000"


class TestVectorizedClock:
    def test_matches_scalar_implementation(self):
        """向量化实现必须与标量实现完全同口径（否则中位数/均值都会偏）。"""
        samples = pd.Series(["0900", "2400", "9999", "", None, "1260", "0005", 830, "2359"])
        minutes, status = to_clock_minutes(samples)
        for idx, raw in samples.items():
            expected, expected_status = hhmm_to_minutes(raw)
            if expected is None:
                assert pd.isna(minutes.loc[idx])
            else:
                assert int(minutes.loc[idx]) == expected
            assert status.loc[idx] == expected_status

    def test_status_counts(self):
        _, status = to_clock_minutes(pd.Series(["0900", "2400", "9999", ""]))
        assert (status == "ok").sum() == 1
        assert (status == "fixed_2400").sum() == 1
        assert (status == "invalid").sum() == 1
        assert (status == "empty").sum() == 1


class TestToNum:
    def test_negative_delay_kept(self):
        """R-03：提前起飞（负延误）必须保留，不能 clip 到 0。"""
        out = to_num(pd.Series(["-15.00", "7.00", ""]))
        assert out.iloc[0] == -15.0
        assert out.iloc[1] == 7.0
        assert pd.isna(out.iloc[2])

    def test_bounds(self):
        out = to_num(pd.Series(["5000", "-500", "10"]), lower=-300, upper=1500)
        assert pd.isna(out.iloc[0]) and pd.isna(out.iloc[1])
        assert out.iloc[2] == 10


class TestCalendar:
    def test_iter_months(self):
        months = iter_months("2023-11", "2024-02")
        assert months == ["2023-11", "2023-12", "2024-01", "2024-02"]

    def test_prev_month(self):
        assert prev_month("2024-01") == "2023-12"
        assert prev_month("2024-07") == "2024-06"

    def test_parse_ym_rejects(self):
        with pytest.raises(Exception):
            parse_ym("2024-13")
        with pytest.raises(Exception):
            parse_ym("2024-1")
