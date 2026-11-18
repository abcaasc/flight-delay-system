"""只跑某一张数仓表的调试脚本。

用法：
    python scripts/build_one.py dwd_weather_hourly 2015
    python scripts/build_one.py dwd_flight_detail 2015-01
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.etl import ads, dwd, dws, dim  # noqa: E402

BUILDERS = {
    "dim": lambda p: dim.run_all("script"),
    "dwd_weather_hourly": lambda p: dwd.build_dwd_weather(p, "script"),
    "dwd_flight_detail": lambda p: dwd.build_dwd_flight(p, "script"),
    "dws_airport_hourly": lambda p: dws.build_dws_airport_hourly(p, "script"),
    "dws_route_daily": lambda p: dws.build_dws_route_daily(p, "script"),
    "dws_delay_network": lambda p: dws.build_dws_delay_network(p, "script"),
    "dws_flight_features": lambda p: dws.build_dws_flight_features(p, "script"),
    "ads_airport_rank": lambda p: ads.build_ads_airport_rank(p, "script"),
    "ads_anomaly_alert": lambda p: ads.build_ads_anomaly_alert(p, "script"),
}


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    table, period = sys.argv[1], sys.argv[2]
    if table not in BUILDERS:
        print(f"未知表：{table}，可选：{list(BUILDERS)}")
        return 2
    app = create_app()
    with app.app_context():
        result = BUILDERS[table](period)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
