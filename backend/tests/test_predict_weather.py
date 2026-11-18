import pandas as pd
import pytest
from app.services import predict_service as service


@pytest.fixture(autouse=True)
def airport_timezone(monkeypatch):
    monkeypatch.setattr(service.stats, 'airport_dim', lambda: {'ATL': {'tz': 'UTC'}})

@pytest.mark.parametrize('day,minutes,observation', [('2024-01-01', 60, '2023-12-31 23:00'), ('2024-02-01', 1440, '2024-02-01 23:00')])
def test_weather_crosses_date_boundary(monkeypatch, day, minutes, observation):
    frame = pd.DataFrame({'obs_hour': [observation], 'vis_mi': [0], 'wind_ms': [0], 'temp_c': [0], 'precip_mm': [0], 'weather_class': [0]})
    monkeypatch.setattr(service.engine, 'read', lambda *a, **kw: frame.copy())
    result = service.actual_weather_window('ATL', pd.Timestamp(day).date(), minutes)
    assert result['tmp_mean_c'] == 0
    assert result['vis_min_mi'] == 0


def test_weather_missing_values_are_finite(monkeypatch):
    frame = pd.DataFrame({'obs_hour': ['2024-02-01 09:00'], **{k: [float('nan')] for k in ['vis_mi', 'wind_ms', 'temp_c', 'precip_mm', 'weather_class']}})
    monkeypatch.setattr(service.engine, 'read', lambda *a, **kw: frame.copy())
    result = service.actual_weather_window('ATL', pd.Timestamp('2024-02-01').date(), 600)
    assert result is None


def test_weather_uses_airport_timezone(monkeypatch):
    monkeypatch.setattr(service.stats, 'airport_dim', lambda: {'ATL': {'tz': 'America/New_York'}})
    frame = pd.DataFrame({'obs_hour': ['2024-01-01 13:00'], 'vis_mi': [2], 'wind_ms': [0],
                          'temp_c': [0], 'precip_mm': [0], 'weather_class': [0]})
    monkeypatch.setattr(service.engine, 'read', lambda *a, **kw: frame.copy())
    result = service.actual_weather_window('ATL', pd.Timestamp('2024-01-01').date(), 600)
    assert result['vis_min_mi'] == 2
