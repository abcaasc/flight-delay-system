"""导出「公网演示版」瘦身数仓。

本地完整数仓约 12 GB，不可能进 Git 仓库；公网演示只需要能撑起看板、航线网络、
航班查询和在线预测的那部分数据。本脚本从完整数仓里挑选 2024 全年（与前端月份
选择器上限一致）的 DWS/ADS 预聚合表、全部维表，再对明细表做一次可复现抽样，
并把每个分区压成单文件，最终体量约 25 MB。

用法（在 backend 目录下）：

    python scripts/export_demo_data.py                 # 默认 2024 年 + 8 万行明细
    python scripts/export_demo_data.py --year 2024 --sample-rows 80000
    python scripts/export_demo_data.py --dst data/demo/warehouse

产物目录结构：

    data/demo/warehouse/
    ├── dim/dim_airport/part-00000.parquet
    ├── dim/dim_airline|dim_date|dim_holiday/part-00000.parquet
    ├── dws/dws_airport_hourly/year_month=2024-01/part-00000.parquet
    ├── dws/dws_route_daily/year_month=2024-01/part-00000.parquet
    ├── ads/ads_airport_rank/year_month=2024-01/part-00000.parquet
    ├── ads/ads_anomaly_alert/year_month=2024-01/part-00000.parquet
    ├── ads/ads_model_metric/part-00000.parquet
    ├── ads/ads_delay_pred/year_month=2024-12/part-00000.parquet   # 抽样
    └── dwd/dwd_flight_detail/year_month=2024-12/part-00000.parquet # 抽样

部署时把 WAREHOUSE_ROOT 指向该目录即可（见仓库根目录 DEPLOY.md）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pyarrow.parquet as pq

try:  # pyarrow.dataset 在 pyarrow>=8 才有
    import pyarrow.dataset as ds
except ImportError:  # pragma: no cover
    ds = None

BACKEND_DIR = Path(__file__).resolve().parent.parent

# 按月分区、整年保留的表（预聚合，单月 0.4–1.2 MB）
YEAR_TABLES = [
    ("dws", "dws_airport_hourly"),
    ("dws", "dws_route_daily"),
    ("ads", "ads_airport_rank"),
    ("ads", "ads_anomaly_alert"),
]

# 不分区的小表
FULL_TABLES = [
    ("dim", "dim_airport"),
    ("dim", "dim_airline"),
    ("dim", "dim_date"),
    ("dim", "dim_holiday"),
    ("ads", "ads_model_metric"),
]

# 明细表：只保留一个月，并抽样（换算法则见 --sample-rows）
SAMPLE_TABLES = [
    ("dwd", "dwd_flight_detail", "2024-12", 80_000),
    ("ads", "ads_delay_pred", "2024-12", 20_000),
]


def _dataset(path: Path):
    assert ds is not None, "需要 pyarrow>=8 才能导出"
    return ds.dataset(str(path), format="parquet")


def _write_one(table, out_dir: Path) -> int:
    """把若干 part 文件合并成一个 parquet，返回字节数。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "part-00000.parquet"
    if target.exists():
        target.unlink()
    pq.write_table(table, target, compression="snappy")
    return target.stat().st_size


def export_year(src: Path, dst: Path, year: int) -> None:
    for layer, table in YEAR_TABLES:
        parts = sorted((src / layer / table).glob(f"year_month={year}-*"))
        if not parts:
            print(f"  [跳过] {layer}/{table}：没有 {year} 年分区")
            continue
        total = 0
        for part in parts:
            table_obj = _dataset(part).to_table()
            total += _write_one(table_obj, dst / layer / table / part.name)
        print(f"  [OK] {layer}/{table:<22} {len(parts):2d} 个分区  {total / 1e6:6.1f} MB")


def export_full(src: Path, dst: Path) -> None:
    for layer, table in FULL_TABLES:
        part = src / layer / table
        if not part.is_dir():
            print(f"  [跳过] {layer}/{table}：目录不存在")
            continue
        size = _write_one(_dataset(part).to_table(), dst / layer / table)
        print(f"  [OK] {layer}/{table:<22}  单表      {size / 1e6:6.1f} MB")


def export_sampled(src: Path, dst: Path, rows: int, seed: int) -> None:
    manifest = dst / "_sampled_flight_months.json"
    sampled = set(json.loads(manifest.read_text(encoding="utf-8"))) if manifest.is_file() else set()
    for layer, table, period, default_rows in SAMPLE_TABLES:
        part = src / layer / table / f"year_month={period}"
        if not part.is_dir():
            print(f"  [跳过] {layer}/{table}：缺少 {period} 分区")
            continue
        need = rows if table == "dwd_flight_detail" else min(rows, default_rows)
        table_obj = _dataset(part).to_table()
        n = table_obj.num_rows
        if table == "dwd_flight_detail":
            sampled.discard(period)
            if n > need:
                sampled.add(period)
        if n > need:
            rng = np.random.default_rng(seed)
            idx = np.sort(rng.choice(n, size=need, replace=False))
            table_obj = table_obj.take(idx)
        size = _write_one(table_obj, dst / layer / table / f"year_month={period}")
        print(f"  [OK] {layer}/{table:<22} {period}  {table_obj.num_rows:>7,} 行  {size / 1e6:6.1f} MB")
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(sorted(sampled)) + "\n", encoding="utf-8")


def copy_models(src: Path, dst: Path) -> None:
    if not src.is_dir():
        print(f"  [跳过] 模型目录不存在：{src}")
        return
    dst.mkdir(parents=True, exist_ok=True)
    total = 0
    same = src.resolve() == dst.resolve()
    for f in sorted(src.iterdir()):
        if f.is_file() and f.suffix in {".pkl", ".json"}:
            if not same:
                shutil.copy2(f, dst / f.name)
            total += f.stat().st_size
    print(f"  [OK] 模型产物 {total / 1e6:.1f} MB（{dst}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="导出公网演示用的瘦身数仓")
    ap.add_argument("--src", default=str(BACKEND_DIR / "data" / "warehouse"), help="完整数仓目录")
    ap.add_argument("--dst", default=str(BACKEND_DIR / "data" / "demo" / "warehouse"), help="演示数仓输出目录")
    ap.add_argument("--models-src", default=str(BACKEND_DIR / "data" / "models"), help="模型产物目录")
    ap.add_argument("--models-dst", default=str(BACKEND_DIR / "data" / "models"), help="演示用模型目录")
    ap.add_argument("--year", type=int, default=2024, help="保留哪一年的 DWS/ADS 预聚合")
    ap.add_argument("--sample-rows", type=int, default=80_000, help="明细表抽样行数")
    ap.add_argument("--seed", type=int, default=42, help="抽样随机种子（保证可复现）")
    args = ap.parse_args()

    src, dst = Path(args.src), Path(args.dst)
    if not src.is_dir():
        print(f"[错误] 数仓目录不存在：{src}", file=sys.stderr)
        return 2

    print(f"源数仓：{src}\n输出目录：{dst}\n")
    export_year(src, dst, args.year)
    export_full(src, dst)
    export_sampled(src, dst, args.sample_rows, args.seed)
    copy_models(Path(args.models_src), Path(args.models_dst))

    size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
    files = sum(1 for f in dst.rglob("*") if f.is_file())
    print(f"\n完成：{files} 个文件，{size / 1e6:.1f} MB → {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
