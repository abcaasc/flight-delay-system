"""在临时数仓中验证真实 BTS 样本链路，不改动现有数据库、数仓及模型。

用法：.venv/Scripts/python.exe scripts/verify_pipeline.py --rows 3000
需要本地公开数据源及 requirements-model.txt；输出 JSON 验证报告。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import time


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=3000)
    parser.add_argument('--periods', nargs='+')
    parser.add_argument('--output', type=Path, default=Path('instance/verification/last_report.json'))
    args = parser.parse_args()
    if args.rows < 100:
        parser.error('--rows 至少为 100')
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    report = {'scope': '真实 BTS 样本，隔离 SQLite/Parquet/模型目录；非全量性能验收', 'checks': []}

    with tempfile.TemporaryDirectory(prefix='fds-verify-') as temporary:
        root = Path(temporary)
        os.environ.update({
            'DATABASE_URL': f'sqlite:///{(root / "verify.db").as_posix()}',
            'WAREHOUSE_ROOT': str(root / 'warehouse'), 'MODEL_ROOT': str(root / 'models'),
            'STAGING_ROOT': str(root / 'staging'), 'QUERY_ENGINE': 'pandas',
            'REDIS_URL': '', 'FLASK_ENV': 'test',
        })
        from app import create_app
        from app.extensions import db
        from app.models import ModelVersion
        from app.etl import ods, dim, dwd, dws, ads
        from app.services import datasource, engine, ingest, model, predict_service, stats, weather_map
        from app.services.cache import invalidate

        def checked(name, action):
            started = time.perf_counter()
            value = action()
            failures = ingest._failures(value)
            if failures:
                raise RuntimeError(f'{name}: {failures}: {value}')
            report['checks'].append({'name': name, 'passed': True,
                                     'seconds': round(time.perf_counter() - started, 4)})
            print(f'PASS {name}', flush=True)
            return value

        application = create_app()
        try:
            with application.app_context():
                available = datasource.available_periods('bts')
                periods = args.periods or available[:3]
                if len(periods) < 1:
                    raise RuntimeError('本地 BTS 数据源没有可用月份')
                report['periods'] = periods
                report['rows_per_month_limit'] = args.rows
                checked('dimensions', dim.run_all)
                stats.airport_dim.cache_clear()
                stats.airline_dim.cache_clear()
                weather_years = set()
                for period in periods:
                    checked(f'ODS {period}', lambda p=period: ods.ingest_bts([p], mode='full', limit=args.rows))
                    year = period[:4]
                    if year not in weather_years:
                        station_map = weather_map.station_airport_map()
                        available_airports = {station_map[p.stem] for p in weather_map.available_station_files(year)}
                        origins = engine.read('ods_bts_flight', columns=['origin'], filters=[('year_month', '=', period)])
                        candidates = origins[origins.origin.isin(available_airports)].origin.value_counts().head(3).index.tolist()
                        if candidates:
                            checked(f'NOAA sample {year}', lambda: dwd.build_dwd_weather(year, airports=set(candidates)))
                            report.setdefault('weather_airports', {})[year] = candidates
                        weather_years.add(year)
                    checked(f'DWD {period}', lambda p=period: dwd.build_dwd_flight(p))
                    checked(f'DWS {period}', lambda p=period: dws.run([p]))
                    checked(f'ADS {period}', lambda p=period: ads.run([p]))
                training = checked('LightGBM training', lambda: model.train_and_evaluate(
                    periods, algo='lightgbm', params={'n_estimators': 30, 'min_child_samples': 20}))
                report['training'] = training
                checked('version-specific batch prediction', lambda: model.predict_and_store(
                    periods[-1], model_ver=training['model_ver']))
                version = db.session.query(ModelVersion).filter_by(model_ver=training['model_ver']).one()
                version.is_online = 1
                db.session.commit()
                sample = engine.read('dws_flight_features', filters=[('year_month', '=', periods[-1])], limit=1).iloc[0]
                prediction = checked('single-flight prediction', lambda: predict_service.predict_single(
                    str(sample.origin), str(sample.dest), str(sample.reporting_airline),
                    str(sample.flight_date), f'{int(sample.dep_hour):02d}00'))
                assert prediction['model_ver'] == training['model_ver']
                report['online_prediction'] = prediction
                invalidate('overview')
                with application.test_client() as client:
                    routes = ['/api/v1/overview/kpi', '/api/v1/overview/trend', '/api/v1/flights?page_size=5',
                              '/api/v1/predict/high-risk']
                    latencies = {}
                    for route in routes:
                        samples = []
                        for _ in range(10):
                            started = time.perf_counter()
                            response = client.get(route)
                            samples.append((time.perf_counter() - started) * 1000)
                            assert response.status_code == 200 and response.get_json()['code'] == 0, response.get_json()
                        latencies[route] = {'max_ms': round(max(samples), 2), 'requests': len(samples)}
                    report['sample_api_latency'] = latencies
                report['weather_note'] = '每年选样本出发机场中有 NOAA 文件的最多 3 站读取观测；不是全部站点覆盖率验收。'
                report['passed'] = True
        finally:
            db.session.remove()
            db.engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {args.output.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
