"""跨服务回归：缓存隔离、作业事务、时序特征与模型版本真实性。"""
from __future__ import annotations

import json
import pickle
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from app.extensions import db
from app.models import AuditLog, ModelVersion
from app.services import cache, engine, ingest, model
from app.etl import dwd, dws
from app.utils import params
from app.utils.response import ApiError
from app.utils.timeutil import local_departure_utc


def test_cache_separates_functions_with_same_namespace(monkeypatch):
    monkeypatch.setattr(cache, 'cache', cache._MemoryCache())

    @cache.json_cached('overview')
    def kpi():
        return {'kind': 'kpi'}

    @cache.json_cached('overview')
    def trend():
        return {'kind': 'trend'}

    assert kpi() == kpi() == {'kind': 'kpi'}
    assert trend() == {'kind': 'trend'}


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf', '1.5'])
def test_integer_rejects_non_integer(app, value):
    with app.test_request_context(query_string={'n': value}):
        with pytest.raises(ApiError):
            params.get_int('n')


@pytest.mark.parametrize('value', ['nan', 'inf', '-inf'])
def test_float_rejects_non_finite(app, value):
    with app.test_request_context(query_string={'n': value}):
        with pytest.raises(ApiError):
            params.get_float('n')


def test_job_failure_is_not_success(app, monkeypatch):
    monkeypatch.setattr(ingest, '_run_job', lambda *a: {'stage': {'status': 'failed'}})
    job = ingest.create_job('dims', 'system', background=False)
    state = ingest.job_status(job['job_id'])
    assert state['status'] == 'failed'
    assert state['err_code'] == 'E_STAGE_FAILED'
    assert ingest.active_jobs() == {}


def test_background_job_uses_own_session(app, monkeypatch):
    done = threading.Event()

    def run(*args):
        return {'stage': {'status': 'success'}}

    original = ingest._release

    def release(dataset):
        original(dataset)
        done.set()

    monkeypatch.setattr(ingest, '_run_job', run)
    monkeypatch.setattr(ingest, '_release', release)
    job = ingest.create_job('dims', 'system')
    db.session.remove()
    assert done.wait(10)
    assert ingest.job_status(job['job_id'])['status'] == 'success'
    db.session.remove()


def test_job_database_failure_releases_lock(app, monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(db.session, 'commit', lambda: (_ for _ in ()).throw(RuntimeError('db unavailable')))
        with pytest.raises(RuntimeError):
            ingest.create_job('dims', 'system', background=False)
    assert ingest.active_jobs() == {}


def test_failed_dwd_stops_downstream(monkeypatch):
    monkeypatch.setattr(ingest.dwd_etl, 'run', lambda *a, **kw: {'part': {'status': 'failed'}})
    monkeypatch.setattr(ingest.dws_etl, 'run', lambda *a, **kw: pytest.fail('DWS should not run'))
    assert ingest._run_etl(['2024-01'], 'test')['failed']


def test_lag_is_calendar_weighted_and_excludes_cancellations():
    frame = pd.DataFrame({
        'origin': ['ATL'] * 5, 'flight_sk': list('abcde'),
        'flight_date': ['2024-01-01', '2024-01-09', '2024-01-09', '2024-01-09', '2024-01-10'],
        'cancelled': [0, 0, 0, 1, 0], 'dep_del15': [0, 1, 1, np.nan, 0],
        'delay_level': [0, 1, 1, -1, 0], 'dep_delay_min': [0, 20, 20, np.nan, 0],
        'chain_available': [0] * 5})
    result = dws._delay_rate_by(dws._flags(frame), ['origin']).set_index('flight_date')
    assert pd.isna(result.loc['2024-01-09', 'rate_7d'])
    assert result.loc['2024-01-10', 'rate_7d'] == 1
    assert result.loc['2024-01-10', 'traffic_prev'] == 2


def test_time_split_keeps_dates_disjoint():
    frame = pd.DataFrame({'flight_date': ['2024-01-01'] * 20 + ['2024-01-02'] * 3 + ['2024-01-03'] * 2,
                          'dep_hour': [8] * 25})
    train, valid, test = model._time_split(frame, (.7, .15, .15))
    assert train.flight_date.max() < valid.flight_date.min() < test.flight_date.min()
    with pytest.raises(ValueError):
        model._time_split(frame.iloc[:20], (.7, .15, .15))


def test_context_never_reads_future(monkeypatch):
    monkeypatch.setattr(engine.BaseEngine, 'available_periods', lambda table: ['2024-01', '2024-12'])
    history = pd.DataFrame({'flight_date': ['2024-01-09', '2024-01-10'], 'origin': ['ATL'] * 2,
                            'dest': ['JFK'] * 2, 'route_key': ['ATL-JFK'] * 2,
                            'reporting_airline': ['AA'] * 2, 'dep_del15': [0, 1], 'cancelled': [0, 0]})
    reads = []

    def read(table, **kwargs):
        reads.append(kwargs['filters'][0][2])
        return history.copy()

    monkeypatch.setattr(engine, 'read', read)
    context = model.feature_context(as_of='2024-01-10')
    assert reads == ['2024-01']
    assert context['origin']['ATL'] == 0


def test_weather_window_includes_last_observation_plus_three_hours():
    weather = pd.DataFrame({'airport_iata': ['ATL'], 'obs_hour': ['2024-01-01 12:00'],
                            **{c: [0.] for c in ['vis_mi', 'wind_ms', 'temp_c', 'dew_c', 'slp_hpa', 'precip_mm', 'weather_class']}})
    result = dwd._window_weather_features(weather).set_index('anchor_hour')
    assert result.loc[pd.Timestamp('2024-01-01 15:00'), 'weather_available'] == 1
    assert result.loc[pd.Timestamp('2024-01-01 12:00'), 'weather_available'] == 0


def test_dst_ambiguous_time_is_unavailable():
    assert pd.isna(local_departure_utc('2024-11-03 01:30', 'America/New_York'))
    assert pd.isna(local_departure_utc('2024-03-10 02:30', 'America/New_York'))


def test_arrow_fallback_preserves_all_filters(monkeypatch):
    import pyarrow.dataset as ds
    reader = engine.PandasEngine()
    monkeypatch.setattr(reader, 'require', lambda table: Path('.'))
    monkeypatch.setattr(ds, 'dataset', lambda *a, **kw: (_ for _ in ()).throw(ValueError('force fallback')))
    monkeypatch.setattr(pd, 'read_parquet', lambda *a: pd.DataFrame({'year_month': ['2024-01', '2024-01'], 'origin': ['ATL', 'JFK']}))
    result = reader.read('dwd_flight_detail', filters=[('origin', '=', 'XXX'), ('year_month', '=', '2024-01')])
    assert result.empty
    assert engine._norm_filters([('origin', 'not in', ['ATL'])])[0][1] == 'not in'


@pytest.mark.parametrize('algo', ['baseline_lr', 'lightgbm'])
def test_training_artifact_predicts_same_version_without_mutating_online(app, monkeypatch, tmp_path, algo):
    if algo == 'lightgbm':
        pytest.importorskip('lightgbm')
    frame = pd.DataFrame({'flight_date': np.repeat(pd.date_range('2024-01-01', periods=30).strftime('%Y-%m-%d'), 10),
                          'dep_hour': np.tile(np.arange(10), 30), 'origin': ['ATL'] * 300,
                          'dep_del15': np.tile([0, 1], 150), 'delay_level': np.tile([0, 1], 150)})
    monkeypatch.setattr(model, 'load_training_frame', lambda periods: frame.copy())
    monkeypatch.setattr(model.Config, 'MODEL_ROOT', tmp_path)
    monkeypatch.setattr(engine, 'write', lambda *a, **kw: {})
    before = model.load_calibration().copy()
    report = model.train_and_evaluate(['2024-01'], algo=algo, params={'n_estimators': 5, 'min_child_samples': 5})
    assert model.load_calibration() == before
    artifact = Path(report['artifact_path'])
    assert artifact.exists()
    result = model.predict_frame(frame, model_ver=report['model_ver'])
    assert result['model_ver'] == report['model_ver']
    assert result['threshold'] == report['calibration']['threshold'] or round(result['threshold'], 4) == report['calibration']['threshold']
    logits = result['base_logit'] + result['contrib'].sum(axis=1).to_numpy()
    np.testing.assert_allclose(1 / (1 + np.exp(-logits)), result['prob'], atol=1e-6)
    row = db.session.query(ModelVersion).filter_by(model_ver=report['model_ver']).one()
    db.session.delete(row)
    db.session.commit()


def test_user_audit_does_not_store_password(app, client, auth):
    from app.models import User
    with app.app_context():
        user = db.session.query(User).filter_by(username='guest').one()
        user_id = user.id
    response = client.put(f'/api/v1/system/users/{user_id}', headers=auth('admin'),
                          json={'display_name': 'Guest', 'password': 'guest123'})
    assert response.get_json()['code'] == 0
    with app.app_context():
        detail = db.session.query(AuditLog).filter_by(action='user_update').order_by(AuditLog.id.desc()).first().detail
        assert 'guest123' not in detail


def test_session_released_after_request(app):
    with app.test_client() as client:
        assert client.get('/').status_code == 200
    assert not db.session.registry.has()


def test_large_column_whitelist_fits_mysql_schema():
    from sqlalchemy import Text
    assert isinstance(ModelVersion.__table__.c.feature_set.type, Text)


def test_invalid_prediction_distance_is_rejected(client):
    for distance in [-1, 'nan', 'inf', []]:
        response = client.post('/api/v1/predict', json={
            'origin': 'ATL', 'dest': 'JFK', 'airline': 'AA', 'flight_date': '2024-01-10',
            'crs_dep_time': '1000', 'distance_mi': distance})
        assert response.status_code == 400


def test_failed_overwrite_keeps_previous_partition(monkeypatch):
    import pyarrow.parquet as pq
    reader = engine.PandasEngine()
    table = 'dws_route_daily'
    reader.write(table, pd.DataFrame({'route_key': ['ATL-JFK'], 'flights': [12]}), '2098-01')
    with monkeypatch.context() as patch:
        patch.setattr(pq, 'write_table', lambda *a, **kw: (_ for _ in ()).throw(OSError('disk full')))
        with pytest.raises(OSError):
            reader.write(table, pd.DataFrame({'route_key': ['ATL-JFK'], 'flights': [99]}), '2098-01')
    result = reader.read(table, filters=[('year_month', '=', '2098-01')])
    assert result['flights'].tolist() == [12]
    reader.drop_partition(table, '2098-01')


def test_short_hhmm_values_do_not_depend_on_row_position():
    from app.etl.common import to_clock_minutes
    samples = pd.Series(['9', '5.0', '10000', 'garbage'] * 30)
    minutes, status = to_clock_minutes(samples)
    for offset in (0, 60, 100):
        assert minutes[offset] == 9
        assert minutes[offset + 1] == 5
        assert pd.isna(minutes[offset + 2])
        assert status[offset + 2] == status[offset + 3] == 'invalid'


def test_empty_in_filter_is_not_unbounded():
    assert engine._norm_filters([('origin', 'in', [])]) == [('origin', 'in', [])]


def test_year_partition_filter_matches_arrow_string_schema():
    assert engine.prepare_filters('dwd_weather_hourly', [('year', '=', 2015)]) == [('year', '=', '2015')]


def test_csv_limits_generator_and_neutralizes_formulas(app):
    from app.api.common import csv_response

    def rows():
        yield {'text': '=1+1', 'delay': -5}
        pytest.fail('export consumed more than max_rows')

    with app.test_request_context():
        text = csv_response(rows(), 'test', max_rows=1).get_data(as_text=True)
        assert "'=1+1,-5" in text


@pytest.mark.parametrize('value', ['2024-02-30', '2024-13-01'])
def test_invalid_calendar_date_returns_400(client, value):
    response = client.get('/api/v1/overview/kpi', query_string={'date_from': value})
    assert response.status_code == 400
    assert response.get_json()['code'] == 422


def test_weather_can_read_source_when_ods_is_absent(monkeypatch):
    class Context:
        def info(self, *args):
            pass

    monkeypatch.setattr(engine, 'read', lambda *a, **kw: (_ for _ in ()).throw(ApiError(521)))
    monkeypatch.setattr(dwd.weather_map, 'available_station_files', lambda year: [])
    assert dwd._read_isd_source('2015', {'station'}, Context()).empty
