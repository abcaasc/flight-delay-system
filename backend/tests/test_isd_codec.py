"""ISD 气象编码解析与 7 类天气编码的单测。"""
from __future__ import annotations

import pandas as pd
import pytest

from app.etl import isd_codec


class TestParseFields:
    def test_wnd_speed_tenths(self):
        direction, speed, gust = isd_codec.parse_wnd("070,1,N,0036,1")
        assert direction == 70
        assert speed == pytest.approx(3.6)
        assert gust is None

    def test_wnd_with_gust(self):
        _, speed, gust = isd_codec.parse_wnd("270,1,N,0050,1,0090,1")
        assert speed == pytest.approx(5.0)
        assert gust == pytest.approx(9.0)

    def test_wnd_missing(self):
        assert isd_codec.parse_wnd("999,9,N,9999,9") == (None, None, None)
        assert isd_codec.parse_wnd(None) == (None, None, None)

    def test_cig(self):
        assert isd_codec.parse_cig("22000,5,9,N") == 22000
        assert isd_codec.parse_cig("99999,9,9,N") is None

    def test_vis(self):
        assert isd_codec.parse_vis("016000,1,9,9") == 16000
        assert isd_codec.parse_vis("999999,9,9,9") is None

    def test_tmp_scale(self):
        assert isd_codec.parse_tmp("+0150,1") == pytest.approx(15.0)
        assert isd_codec.parse_tmp("-0075,1") == pytest.approx(-7.5)
        assert isd_codec.parse_tmp("+9999,1") is None

    def test_slp(self):
        assert isd_codec.parse_slp("10172,1") == pytest.approx(1017.2)
        assert isd_codec.parse_slp("99999,9") is None

    def test_aa1_both_layouts(self):
        """AA1 有两种真实写法：深度在第二位或第三位。"""
        assert isd_codec.parse_aa1("01,0000,9,5") == pytest.approx(0.0)
        assert isd_codec.parse_aa1("99,9,+01250,1,99,9") == pytest.approx(125.0)
        assert isd_codec.parse_aa1(None) is None

    def test_unit_conversion(self):
        assert isd_codec.meters_to_miles(1609.344) == pytest.approx(1.0, abs=1e-3)
        assert isd_codec.meters_to_feet(1000) == pytest.approx(3280.8, abs=0.2)


class TestWeatherClass:
    def test_normal(self):
        assert isd_codec.classify_weather(16000, 8000, 3.0, 20.0, 0.0) == 0

    def test_fog_by_visibility(self):
        assert isd_codec.classify_weather(800, 300, 2.0, 5.0, 0.0) == 4

    def test_storm_by_wind(self):
        assert isd_codec.classify_weather(16000, 8000, 15.0, 20.0, 0.0) == 5

    def test_rain_by_precip(self):
        assert isd_codec.classify_weather(16000, 8000, 3.0, 20.0, 2.5) == 1

    def test_snow_below_zero(self):
        assert isd_codec.classify_weather(16000, 8000, 3.0, -3.0, 2.0) == 6

    def test_thunderstorm_from_remarks(self):
        assert isd_codec.classify_weather(16000, 8000, 3.0, 20.0, 0.0,
                                          "METAR TSRA BKN020") == 3

    def test_cloud_low_ceiling(self):
        assert isd_codec.classify_weather(16000, 500, 3.0, 20.0, 0.0) == 2

    def test_priority_thunderstorm_beats_fog(self):
        assert isd_codec.classify_weather(500, 200, 3.0, 20.0, 5.0, "TS") == 3

    def test_vectorized_equals_scalar(self):
        vis = pd.Series([16000.0, 800.0, 16000.0, 16000.0, 500.0])
        ceil = pd.Series([8000.0, 300.0, 8000.0, 8000.0, 500.0])
        wind = pd.Series([3.0, 2.0, 15.0, 3.0, 3.0])
        temp = pd.Series([20.0, 5.0, 20.0, -3.0, 20.0])
        precip = pd.Series([0.0, 0.0, 0.0, 2.0, 0.0])
        remarks = pd.Series(["", "", "", "", ""])
        vector = isd_codec.classify_vectorized(vis, ceil, wind, temp, precip, remarks).tolist()
        scalar = [isd_codec.classify_weather(v, c, w, t, p, r) for v, c, w, t, p, r in
                  zip(vis, ceil, wind, temp, precip, remarks)]
        assert vector == scalar
