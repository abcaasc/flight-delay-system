import pandas as pd

from app.config import Config
from app.services import engine


def _flight(date, number, **fields):
    return {
        "flight_date": date, "flight_number": number, "reporting_airline": "DL",
        "origin": "MSP", "dest": "DTW", "cancelled": 0, "diverted": 0, "arr_del15": 1,
        **fields,
    }


def test_reason_summary_filters_exact_dates_and_counts_only_positive_minutes(client, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "WAREHOUSE_ROOT", str(tmp_path))
    nov = [
        _flight("2024-11-29", "1", carrier_delay=999),
        _flight("2024-11-30", "2", carrier_delay=10, nas_delay=5, weather_delay=0),
        _flight("2024-11-30", "3", origin="ORD", carrier_delay=999),
        _flight("2024-11-30", "4", reporting_airline="AA", carrier_delay=999),
    ]
    dec = [
        _flight("2024-12-01", "5", weather_delay=30, nas_delay=-5),
        _flight("2024-12-01", "6", arr_del15=0, carrier_delay=999),
        _flight("2024-12-01", "7", cancelled=1, carrier_delay=999),
        _flight("2024-12-01", "8", diverted=1, carrier_delay=999),
        _flight("2024-12-01", "9"),
        _flight("2024-12-02", "10", carrier_delay=999),
    ]
    for month, rows in (("2024-11", nov), ("2024-12", dec)):
        engine.write("ods_bts_flight", pd.DataFrame(rows), partition_value=month)

    response = client.get("/api/v1/overview/reasons", query_string={
        "date_from": "2024-11-30", "date_to": "2024-12-01", "airports": "msp", "airlines": "dl",
    })
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["arrival_delayed_flights"] == 3
    assert data["reported_flights"] == 2
    assert data["total_minutes"] == 45
    assert {item["key"]: (item["minutes"], item["flights"]) for item in data["items"]} == {
        "carrier_delay": (10, 1), "weather_delay": (30, 1), "nas_delay": (5, 1),
        "security_delay": (0, 0), "late_aircraft_delay": (0, 0),
    }


def test_flight_detail_reports_reasons_and_missing_coverage(client, monkeypatch, tmp_path):
    monkeypatch.setattr(Config, "WAREHOUSE_ROOT", str(tmp_path))
    date = "2024-11-24"
    raw = [
        _flight(date, "1519", carrier_delay=14, late_aircraft_delay=9, weather_delay=0),
        _flight(date, "1520", arr_del15=0),
        _flight(date, "1521"),
    ]
    detail = [
        _flight(date, "1519", flight_sk="reported", dep_delay_min=32, arr_delay_min=23),
        _flight(date, "1520", flight_sk="not-applicable", dep_delay_min=20, arr_delay_min=10, arr_del15=0),
        _flight(date, "1521", flight_sk="not-reported", arr_delay_min=16),
        _flight(date, "1522", flight_sk="missing-source", arr_delay_min=16),
    ]
    engine.write("ods_bts_flight", pd.DataFrame(raw), partition_value="2024-11")
    engine.write("dwd_flight_detail", pd.DataFrame(detail), partition_value="2024-11")

    for key, expected in (("reported", "reported"), ("not-applicable", "not_applicable"),
                          ("not-reported", "not_reported"), ("missing-source", "source_unavailable")):
        response = client.get(f"/api/v1/flights/{key}", query_string={"flight_date": date})
        assert response.status_code == 200
        causes = response.get_json()["data"]["arrival_delay_reasons"]
        assert causes["status"] == expected
        values = [(item["key"], item["minutes"]) for item in causes["items"]]
        assert values == ([("carrier_delay", 14), ("late_aircraft_delay", 9)] if key == "reported" else [])
    assert client.get("/api/v1/flights/reported").status_code == 200
