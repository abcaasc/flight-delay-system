"""在当前 MySQL/数仓验证一个完整月份，无行数采样；不替代十年全量任务。"""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from full_pipeline import save, OUTPUT
from app import create_app
from app.extensions import db
from app.models import ModelVersion
from app.etl import dwd, dws, ads
from app.services import engine, model, stats, ingest
from app.services.cache import invalidate

report = {'scope': '2015-01 完整月、全部可映射 2015 年气象站；无行数上限；不代表 120 个月已完成', 'checks': []}

def checked(name, action):
    start = time.time()
    result = action()
    if ingest._failures(result):
        raise RuntimeError(f'{name} failed: {result}')
    report['checks'].append({'name': name, 'seconds': round(time.time()-start,2), 'passed': True})
    save(OUTPUT / 'full-month-pilot.json', report)
    print(f'PASS {name}', flush=True)
    return result

with create_app().app_context():
    try:
        checked('full_year_weather', lambda:dwd.build_dwd_weather('2015', operator='full_month_test'))
        checked('full_month_dwd', lambda:dwd.build_dwd_flight('2015-01', operator='full_month_test'))
        checked('full_month_dws', lambda:dws.run(['2015-01'], operator='full_month_test'))
        checked('full_month_ads', lambda:ads.run(['2015-01'], operator='full_month_test'))
        training = checked('full_month_lightgbm', lambda:model.train_and_evaluate(['2015-01'],algo='lightgbm',params={'n_estimators':60},operator='full_month_test'))
        report['training'] = training
        report['prediction'] = checked('full_month_prediction', lambda:model.predict_and_store('2015-01',model_ver=training['model_ver'],operator='full_month_test'))
        # 仅将已经完成全月预测的验收模型设为当前开发环境模型。
        db.session.query(ModelVersion).update({ModelVersion.is_online:0})
        db.session.query(ModelVersion).filter_by(model_ver=training['model_ver']).one().is_online=1
        db.session.commit()
        invalidate('overview')
        report['overview'] = stats.overview_kpi('2015-01-01','2015-01-31')
        report['partitions'] = {table:engine.BaseEngine.partitions(table) for table in ('ods_bts_flight','dwd_flight_detail','dws_flight_features','ads_delay_pred')}
        report['status'] = 'success'
    except Exception as exc:
        report.update(status='failed',error=str(exc))
        raise
    finally:
        save(OUTPUT / 'full-month-pilot.json',report)
