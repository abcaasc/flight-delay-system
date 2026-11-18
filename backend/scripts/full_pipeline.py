"""全量接入与验证，按步骤隔离进程并持久化断点；不对入湖/ETL设置行数上限。

运行：.venv/Scripts/python.exe scripts/full_pipeline.py
恢复：重复同一命令；查看 instance/full_pipeline/report.json 和 pipeline.log。
模型验证使用最近三个完整月份的全部有效样本，随后预测全部月份。
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'instance' / 'full_pipeline'
REPORT = OUTPUT / 'report.json'
sys.path.insert(0, str(ROOT))


def save(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    temporary.replace(path)


def worker(step):
    from app import create_app
    from app.config import Config
    from app.extensions import db
    from app.etl import ads, dim, dwd, dws, ods
    from app.services import datasource, engine, model, ingest

    app = create_app()
    with app.app_context():
        kind, _, period = step.partition(':')
        if kind == 'inventory':
            inventory = datasource.dataset_status()
            verification = {key: datasource.verify_dataset(key) for key in datasource.DATASETS}
            # 元数据清单可能列出它自身写入前的哈希；所有真实数据文件必须匹配。
            failed = [key for key, result in verification.items()
                      if not result['passed'] and key not in ('manifest', 'dictionary')]
            save(OUTPUT / 'source-verification.json', verification)
            if failed:
                raise RuntimeError(f'源数据校验失败：{failed}；见 source-verification.json')
            from app.etl.common import upsert_manifest
            references = []
            for key in datasource.DATASETS:
                for item in datasource.scan_files(key):
                    path = Config.DATA_ROOT / item['rel_path']
                    checksum = datasource.file_md5(path)
                    upsert_manifest(key, item['period'], item['file_name'], item['rel_path'], checksum, item['bytes'], 0)
                    references.append({**item, 'md5': checksum,
                                       'access': 'warehouse' if key in ('bts','isd','weatherevents','ourairports','openflights') else 'source-reference'})
            save(OUTPUT / 'source-catalog.json', references)
            result = {'inventory': inventory, 'registered_files': len(references),
                      'business_database': db.engine.url.render_as_string(hide_password=True),
                      'warehouse': str(Config.WAREHOUSE_ROOT), 'row_limit': None}
            seed = app.test_cli_runner().invoke(args=['seed-users'])
            if seed.exit_code:
                raise RuntimeError('初始化应用账户失败')
        elif kind == 'dims':
            result = dim.run_all(operator='full_pipeline')
        elif kind == 'bts':
            result = ods.ingest_bts([period], mode='full', limit=None, operator='full_pipeline')
        elif kind == 'isd':
            result = ods.ingest_isd([period], mode='full', max_stations=None, batch_files=5, operator='full_pipeline')
            if not ingest._failures(result):
                from app.models import FileManifest
                rows = engine.read('ods_isd_weather', columns=['src_file'], filters=[('year','=',period)])['src_file'].value_counts()
                expected = {f['file_name'] for f in datasource.scan_files('isd') if f['period'] == period}
                if expected != set(rows.index):
                    raise RuntimeError(f'ISD {period} 站点缺失：{expected-set(rows.index)}')
                for name, count in rows.items():
                    db.session.query(FileManifest).filter_by(dataset='isd',period=period,file_name=str(name)).update({'row_count':int(count)})
                db.session.commit()
        elif kind == 'weather':
            result = dwd.build_dwd_weather(period, operator='full_pipeline')
        elif kind == 'events':
            result = ods.ingest_weather_events(mode='full', max_rows=None, chunk_size=100000, operator='full_pipeline')
            if not ingest._failures(result):
                source_periods = datasource.available_periods('bts')
                source_years = datasource.available_periods('isd')
                if engine.BaseEngine.available_periods('ods_bts_flight') != source_periods:
                    raise RuntimeError('BTS 原始月份分区不完整')
                if engine.BaseEngine.available_periods('ods_isd_weather') != source_years:
                    raise RuntimeError('NOAA 原始年份分区不完整')
                totals = {}
                for table in ('ods_bts_flight','ods_isd_weather','ods_weather_event'):
                    partitions = engine.BaseEngine.partitions(table)
                    totals[table] = {'partitions':len(partitions),'rows':sum(p['rows'] for p in partitions),
                                     'bytes':sum(p['bytes'] for p in partitions)}
                    if not totals[table]['rows']:
                        raise RuntimeError(f'{table} 原始入库结果为空')
                from app.models import FileManifest
                for item in datasource.scan_files('bts'):
                    db.session.query(FileManifest).filter_by(dataset='bts',period=item['period'],file_name=item['file_name']).update({'rel_path':item['rel_path']})
                db.session.commit()
                save(OUTPUT/'ingestion-complete.json',{
                    'status':'success','completed_at':datetime.now().isoformat(timespec='seconds'),
                    'row_limit':None,'warehouse_raw':totals,
                    'source_files':sum(len(datasource.scan_files(key)) for key in datasource.DATASETS),
                    'business_database':'MySQL / flight_delay',
                    'note':'原始数据已全量接入；DWD/DWS/ADS、十年批量预测和最终验收独立跟踪，不以本报告宣称全部加工完成。',
                })
        elif kind == 'dwd':
            result = dwd.build_dwd_flight(period, limit=None, operator='full_pipeline')
        elif kind == 'dws':
            result = dws.run([period], operator='full_pipeline')
        elif kind == 'ads':
            result = ads.run([period], operator='full_pipeline')
        elif kind == 'train':
            periods = datasource.available_periods('bts')[-3:]
            result = model.train_and_evaluate(periods, algo='lightgbm', operator='full_pipeline')
            result['training_periods'] = periods
            result['training_scope'] = '最近三个完整月份全部有效样本；按日期隔离训练/验证/测试；非十年全量训练'
        elif kind == 'predict':
            training = json.loads((OUTPUT / 'steps' / 'train.json').read_text(encoding='utf-8'))
            result = model.predict_and_store(period, model_ver=training['model_ver'], operator='full_pipeline')
        elif kind == 'validate':
            from app.models import ModelVersion
            from app.services.cache import invalidate
            from app.services import predict_service
            periods = datasource.available_periods('bts')
            required = ['ods_bts_flight', 'dwd_flight_detail', 'dws_airport_hourly',
                        'dws_route_daily', 'dws_flight_features', 'ads_airport_rank', 'ads_delay_pred']
            counts = {}
            for table in required:
                actual = engine.BaseEngine.available_periods(table)
                if set(actual) != set(periods):
                    raise RuntimeError(f'{table} 分区不完整：{set(periods)-set(actual)}')
                counts[table] = {'partitions': len(actual), 'rows': sum(p['rows'] for p in engine.BaseEngine.partitions(table))}
            partition_rows = {table:{p['value']:p['rows'] for p in engine.BaseEngine.partitions(table)} for table in required}
            for p in periods:
                detail = partition_rows['dwd_flight_detail'][p]
                if not (0 < detail <= partition_rows['ods_bts_flight'][p]):
                    raise RuntimeError(f'{p} ODS/DWD 行数对账失败')
                if detail != partition_rows['dws_flight_features'][p] or detail != partition_rows['ads_delay_pred'][p]:
                    raise RuntimeError(f'{p} 明细/特征/预测行数不一致')
            training = json.loads((OUTPUT / 'steps' / 'train.json').read_text(encoding='utf-8'))
            version = db.session.query(ModelVersion).filter_by(model_ver=training['model_ver']).one()
            db.session.query(ModelVersion).update({ModelVersion.is_online: 0})
            version.is_online = 1
            db.session.commit()
            checks = []
            with app.test_client() as client:
                for p in (periods[0], periods[len(periods)//2], periods[-1]):
                    for route in ('overview', 'network', 'flights?page_size=5'):
                        separator = '&' if '?' in route else '?'
                        path = f'/api/v1/{route}{separator}date_from={p}-01&date_to={p}-28'
                        started = time.perf_counter()
                        response = client.get(path)
                        if response.status_code != 200 or response.get_json()['code'] != 0:
                            raise RuntimeError(f'接口验证失败：{path}: {response.get_json()}')
                        checks.append({'path': path, 'status': 200, 'seconds': round(time.perf_counter()-started,3)})
                for path in ('/api/v1/crossdomain/china?top=5', '/api/v1/crossdomain/compare', '/api/v1/predict/high-risk?top=5'):
                    response = client.get(path)
                    if response.status_code != 200 or response.get_json()['code'] != 0:
                        raise RuntimeError(f'跨域或预测接口验证失败：{path}: {response.get_json()}')
                    checks.append({'path': path, 'status': 200})
            for namespace in ('overview','airport','network','predict'):
                invalidate(namespace)
            # 真实 MySQL 服务和浏览器端验收；任何失败都保留日志且阻止整条流程宣称成功。
            import requests
            if requests.get('http://127.0.0.1:5000/api/v1/system/health',timeout=10).status_code != 200:
                raise RuntimeError('验收所需的本机 Flask 服务未运行')
            integration = []
            with tempfile.TemporaryDirectory(dir=OUTPUT, prefix='browser-temp-') as browser_temp:
                # Chrome 需要可写的临时目录来创建隔离的浏览器配置。
                browser_env = {**os.environ, 'TMP': browser_temp, 'TEMP': browser_temp, 'TMPDIR': browser_temp}
                for name, command, cwd, env in (
                    ('api-smoke', [sys.executable, 'scripts/smoke_test.py'], ROOT, None),
                    ('browser', ['node', 'node_modules/@playwright/test/cli.js', 'test'], ROOT.parent/'frontend', browser_env),
                ):
                    with (OUTPUT / f'{name}.log').open('w', encoding='utf-8') as log:
                        completed = subprocess.run(command, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
                    integration.append({'name': name, 'returncode': completed.returncode})
                    if completed.returncode:
                        raise RuntimeError(f'{name} 验收失败，见 {name}.log')
            import numpy as np
            import pandas as pd
            references = []
            for item in datasource.scan_files('iem'):
                frame = pd.read_csv(Config.DATA_ROOT / item['rel_path'], low_memory=False)
                if frame.empty:
                    raise RuntimeError(f'IEM 文件为空：{item["file_name"]}')
                references.append({'file': item['rel_path'], 'rows': len(frame), 'columns': list(frame.columns)})
            for item in datasource.scan_files('stpn'):
                array = np.load(Config.DATA_ROOT / item['rel_path'], mmap_mode='r', allow_pickle=False)
                if not array.size:
                    raise RuntimeError(f'STPN 数组为空：{item["file_name"]}')
                references.append({'file': item['rel_path'], 'shape': list(array.shape), 'dtype': str(array.dtype)})
            result = {'counts': counts, 'checks': checks, 'integration_tests':integration, 'reference_validation': references,
                      'model_ver': training['model_ver'], 'passed': True,
                      'evaluation_note': '历史批量结果为回溯评分；模型指标只来自按日期隔离的测试集，不将回溯评分当作时间外推评估。'}
        else:
            raise ValueError(f'未知步骤：{step}')
        failures = ingest._failures(result)
        if failures:
            save(OUTPUT / 'steps' / f'{step.replace(":", "_")}-failed.json', result)
            raise RuntimeError(f'{step} 作业报告失败：{failures}')
        # 某些旧作业在单文件失败时仍返回 success；检查死信，禁止以部分入湖冒充全量成功。
        from app.models import IngestFailed
        def job_ids(value):
            if isinstance(value, dict):
                return ([value['job_id']] if value.get('job_id') else []) + [j for v in value.values() for j in job_ids(v)]
            return []
        ids = job_ids(result)
        if ids and db.session.query(IngestFailed).filter(IngestFailed.job_id.in_(ids)).count():
            raise RuntimeError(f'{step} 存在文件接入死信，拒绝标记全量完成')
        save(OUTPUT / 'steps' / f'{step.replace(":", "_")}.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--step')
    parser.add_argument('--status', action='store_true', help='只查看当前运行状态')
    args = parser.parse_args()
    if args.step:
        worker(args.step)
        return 0
    if args.status:
        payload = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {'status': 'not_started'}
        print(json.dumps({k: payload.get(k) for k in ('status','current_step','completed_steps','total_steps','started_at','updated_at','finished_at','error')}, ensure_ascii=False, indent=2))
        return 0
    OUTPUT.mkdir(parents=True, exist_ok=True)
    lock = (OUTPUT / 'pipeline.lock').open('a+b')
    lock.seek(0)
    lock.write(b'1')
    lock.flush()
    lock.seek(0)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('已有全量流程运行，请使用 --status 查看进度。', flush=True)
        return 1
    from app.services import datasource
    from app.config import Config
    Config.WAREHOUSE_ROOT.mkdir(parents=True, exist_ok=True)
    periods = datasource.available_periods('bts')
    years = datasource.available_periods('isd')
    if len(periods) != 120:
        raise RuntimeError(f'预期 120 个月，实际 {len(periods)}')
    # 原始资料先全部入湖；之后各年气象、逐月加工，最后建模与全月预测。
    steps = ['inventory', 'dims'] + [f'bts:{p}' for p in periods]
    steps += [f'isd:{year}' for year in years] + ['events']
    for year in years:
        steps += [f'weather:{year}']
        for p in periods:
            if p.startswith(year):
                steps += [f'dwd:{p}', f'dws:{p}', f'ads:{p}']
    steps += ['train'] + [f'predict:{p}' for p in periods] + ['validate']
    report = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {
        'started_at': datetime.now().isoformat(timespec='seconds'), 'steps': {},
        'data_root': str(Config.DATA_ROOT), 'periods': periods, 'row_limit': None,
        'training_scope': '最近三个完整月份的全部有效样本，随后预测全部 120 个月',
    }
    if report.get('status') == 'running' and report.get('pid') != os.getpid() and os.name == 'nt':
        existing = subprocess.run(['tasklist','/FI',f'PID eq {report.get("pid", 0)}','/FO','CSV','/NH'], capture_output=True, text=True)
        if f'"{report.get("pid")}"' in existing.stdout:
            print('已检测到运行中的全量任务，请使用 --status 查看。', flush=True)
            return 1
    report.pop('error', None)
    report.update(status='running', pid=os.getpid(), total_steps=len(steps))
    try:
        for step in steps:
            if report['steps'].get(step, {}).get('status') == 'success':
                continue
            if shutil.disk_usage(Config.WAREHOUSE_ROOT).free < 10*1024**3:
                raise RuntimeError('数仓所在盘剩余空间小于 10 GiB；保留断点后停止')
            started = time.time()
            report.update(current_step=step, updated_at=datetime.now().isoformat(timespec='seconds'))
            report['steps'][step] = {'status': 'running', 'started_at': report['updated_at']}
            save(REPORT, report)
            print(f'START {step}', flush=True)
            completed = subprocess.run([sys.executable, '-u', str(Path(__file__).resolve()), '--step', step], cwd=ROOT)
            entry = report['steps'][step]
            entry.update(status='success' if completed.returncode == 0 else 'failed', seconds=round(time.time()-started,2), returncode=completed.returncode)
            report['completed_steps'] = sum(v.get('status') == 'success' for v in report['steps'].values())
            save(REPORT, report)
            if completed.returncode:
                raise RuntimeError(f'{step} 失败，退出码 {completed.returncode}')
            print(f'DONE {step} {entry["seconds"]}s', flush=True)
        report.update(status='success', finished_at=datetime.now().isoformat(timespec='seconds'))
        save(REPORT, report)
        return 0
    except Exception as exc:
        report.update(status='failed', error=str(exc), updated_at=datetime.now().isoformat(timespec='seconds'))
        save(REPORT, report)
        print(f'FAILED {exc}', flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
