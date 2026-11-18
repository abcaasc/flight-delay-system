"""全量运行使用的低内存气象聚合与白名单训练读取回归。"""
import pandas as pd
import pytest
from app.etl import dwd
from app.etl.common import DropCounter
from app.services import model


class Context:
    drops = DropCounter()


def test_hourly_weather_mode_tie_and_numeric_aggregation(monkeypatch):
    raw = pd.DataFrame({'station_id':['a','a','a','b'], 'date':['2024-01-01T01:00:00','2024-01-01T01:20:00','2024-01-01T02:00:00','2024-01-01T01:00:00']})
    parsed = pd.DataFrame({'vis_mi':[5,2,9,8], 'ceil_ft':[1000]*4, 'wind_ms':[1,7,3,4],
        'gust_ms':[2]*4,'wind_dir':[30]*4,'temp_c':[10,20,30,40], 'dew_c':[4]*4,
        'slp_hpa':[1000]*4,'precip_mm':[.1,.2,.3,.4],'weather_class':[2,1,0,4]})
    monkeypatch.setattr(dwd,'_parse_isd_frame',lambda frame:parsed)
    result=dwd._aggregate_weather(raw,{'a':'ORD','b':'LAX'},Context())
    assert len(result)==3
    hour=result[(result.airport_iata=='ORD')&(result.obs_hour=='2024-01-01 01:00')].iloc[0]
    assert hour.weather_class==1  # 众数同频取较小值，与原 mode().iloc[0] 一致
    assert hour.wind_ms==7
    assert hour.vis_mi==2
    assert hour.temp_c==15
    assert hour.precip_mm==pytest.approx(.3)


def test_training_projection_preserves_all_features_and_labels(monkeypatch):
    requested=[]
    def read(table,columns,filters):
        requested.extend(columns)
        return pd.DataFrame({'flight_date':['2024-01-01'],'origin':['ORD'],'delay_level':[0],
                             'origin_traffic_24h':[123], 'dep_del15':[0]})
    monkeypatch.setattr(model.engine,'read',read)
    result=model.load_training_frame(['2024-01'])
    assert set(model.MODEL_FEATURES).issubset(requested)
    assert 'flight_sk' not in requested
    assert 'delay_level' in requested
    assert model._feature_frame(result).origin_traffic_prev_day.iloc[0]==123
