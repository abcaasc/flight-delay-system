"""从 ORM 元数据生成 MySQL 8.0 建表脚本（保证 DDL 与模型永远一致）。

用法：python scripts/gen_sql.py
输出：sql/mysql_schema.sql
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects import mysql  # noqa: E402
from sqlalchemy.schema import CreateIndex, CreateTable  # noqa: E402

from app.models import Base  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "sql" / "mysql_schema.sql"

HEADER = """-- ============================================================
-- 基于 Spark 的航班延误预测与航线网络态势可视化系统
-- MySQL 8.0 建表脚本（由 scripts/gen_sql.py 从 ORM 元数据生成，勿手改）
-- 用法：mysql -u root -p < mysql_schema.sql
-- ============================================================
CREATE DATABASE IF NOT EXISTS flight_delay
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
USE flight_delay;

"""


def main() -> int:
    dialect = mysql.dialect()
    lines = [HEADER]
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=dialect)).strip()
        lines.append(ddl.rstrip(";") + "\n  ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                     "COMMENT='{}';\n".format(_safe(table.comment or table.name)))
        for index in table.indexes:
            lines.append(str(CreateIndex(index).compile(dialect=dialect)).strip() + ";\n")
        lines.append("\n")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(lines), encoding="utf-8")
    print(f"已生成 {OUT}（{len(Base.metadata.sorted_tables)} 张表）")
    return 0


def _safe(text: str) -> str:
    return str(text).replace("'", "''")[:200]


if __name__ == "__main__":
    raise SystemExit(main())
