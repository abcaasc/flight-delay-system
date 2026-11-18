"""网络筛选回归：日期口径及机场筛选必须在航线截断前生效。"""
import pandas as pd
import pytest
from app.services import stats


@pytest.fixture
def network_data(monkeypatch):
    node_rows = [
        {"airport_iata": "ORD", "stat_date": "2015-01-01", "flights": 10, "dep_del15_cnt": 2, "cancelled_cnt": 0, "dep_delay_sum_min": 100},
        {"airport_iata": "ORD", "stat_date": "2015-01-02", "flights": 20, "dep_del15_cnt": 10, "cancelled_cnt": 0, "dep_delay_sum_min": 400},
        {"airport_iata": "LAX", "stat_date": "2015-01-01", "flights": 100, "dep_del15_cnt": 10, "cancelled_cnt": 0, "dep_delay_sum_min": 500},
    ]
    route_rows = [
        {"origin": "ORD", "dest": "LAX", "route_key": "ORD-LAX", "stat_date": "2015-01-01", "flights": 10, "dep_del15_cnt": 2, "cancelled_cnt": 0, "dep_delay_sum_min": 100, "distance_mi": 1000},
        {"origin": "ORD", "dest": "LAX", "route_key": "ORD-LAX", "stat_date": "2015-01-02", "flights": 20, "dep_del15_cnt": 10, "cancelled_cnt": 0, "dep_delay_sum_min": 400, "distance_mi": 1000},
        {"origin": "LAX", "dest": "ORD", "route_key": "LAX-ORD", "stat_date": "2015-01-01", "flights": 100, "dep_del15_cnt": 10, "cancelled_cnt": 0, "dep_delay_sum_min": 500, "distance_mi": 1000},
    ]
    monkeypatch.setattr(stats, "_read_periods", lambda table, *a, **kw: pd.DataFrame(node_rows if table == "dws_airport_hourly" else route_rows))
    monkeypatch.setattr(stats, "airport_dim", lambda: {})


def test_network_filters_routes_by_exact_date(network_data):
    result = stats.network_graph.__wrapped__("2015-01-01", "2015-01-01")
    route = next(l for l in result["links"] if l["source"] == "ORD")
    assert route["flights"] == 10
    assert route["delay_rate"] == .2
    assert result["summary"]["flights"] == 110


def test_airport_scope_applies_before_top_routes(network_data):
    result = stats.network_graph.__wrapped__("2015-01-01", "2015-01-02", top_routes=1, airports=("ORD",))
    assert len(result["links"]) == 1
    assert result["links"][0]["source"] == "ORD"
    assert result["links"][0]["flights"] == 30
    assert result["summary"]["flights"] == 30
    assert result["summary"]["airports"] == 1
    assert {n["id"] for n in result["nodes"]} == {"ORD", "LAX"}  # 邻居保留地理上下文


def test_network_endpoint_passes_airport_filter(client, monkeypatch):
    captured = []
    monkeypatch.setattr(stats, "network_graph", lambda *args: captured.append(args) or {"nodes": [], "links": []})
    response = client.get("/api/v1/network?date_from=2015-01-01&date_to=2015-01-31&airports=ord,lax")
    assert response.status_code == 200
    assert captured[0][-1] == ("ORD", "LAX")
