"""数仓巡检脚本：查看四层十七表的就绪状态、行数、分区与小文件情况。

用法：python scripts/inspect_warehouse.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.services import engine  # noqa: E402


def main() -> int:
    app = create_app()
    with app.app_context():
        rows = engine.table_overview()
        header = f"{'表':24s} {'层':4s} {'就绪':4s} {'行数':>10s} {'分区':>4s} {'文件数':>5s} {'体积MB':>9s}"
        print(header)
        print("-" * len(header))
        total_rows = total_mb = 0
        for row in rows:
            parts = engine.BaseEngine.partitions(row["table"]) if row["ready"] else []
            files = sum(p["files"] for p in parts)
            mb = row["bytes"] / 1024 / 1024
            total_rows += row["row_count"]
            total_mb += mb
            flag = "✅" if row["ready"] else "—"
            print(f"{row['table']:24s} {row['layer']:4s} {flag:4s} "
                  f"{row['row_count']:>10d} {row['partitions']:>4d} {files:>5d} {mb:>9.2f}")
        print("-" * len(header))
        print(f"{'合计':24s} {'':4s} {'':4s} {total_rows:>10d} {'':4s} {'':5s} {total_mb:>9.2f}")
        print(f"\n引擎：{engine.get_engine().name}")
        latest = engine.BaseEngine.latest_period("dws_airport_hourly")
        print(f"最新统计期：{latest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
