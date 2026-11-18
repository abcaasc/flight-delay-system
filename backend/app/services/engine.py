"""数仓访问引擎（双实现）。

* PandasEngine —— pyarrow dataset + 分区裁剪，单机计算降级路径
* SparkEngine  —— Spark SQL 读取同一套 Parquet 目录，用于十万级以上聚合

两者共享同一份目录布局与表契约（catalog.TABLES），所以 ETL 用哪个引擎写入、
API 用哪个引擎读取都不影响正确性。

目录布局（与《数据接入设计说明》§二 一致）：
    {WAREHOUSE_ROOT}/{layer}/{table}/{partition_col}={value}/*.parquet
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
import uuid
import threading
from functools import wraps
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from app.config import Config
from app.services.catalog import TABLES, TableDef, get_table
from app.utils.logger import get_logger
from app.utils.response import ApiError, ErrCode

log = get_logger(__name__)

FilterTuple = tuple[str, str, object]
_WAREHOUSE_LOCK = threading.RLock()


def _warehouse_locked(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        with _WAREHOUSE_LOCK:
            return fn(*args, **kwargs)
    return wrapper


def _partition_path(root: Path, column: str, value: str) -> Path:
    value = str(value)
    if not value or value in {".", ".."} or any(c in value for c in '/\\:'):
        raise ValueError("非法分区值")
    return root / f"{column}={value}"


def _norm_filters(filters: Iterable[FilterTuple] | None) -> list[FilterTuple]:
    out: list[FilterTuple] = []
    for item in filters or ():
        if item is None:
            continue
        col, op, value = item
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            values = [v for v in value if v is not None]
            out.append((col, "not in" if op == "not in" else "in", list(values)))
        else:
            out.append((col, op, value))
    return out


def prepare_filters(table_name: str, filters: Iterable[FilterTuple] | None) -> list[FilterTuple]:
    """按表契约把分区键的取值转成声明的类型。

    Parquet 分区列由目录名推断，落盘后是字符串；但有些表（如 dwd_weather_hourly.year）
    声明为 int，写入时是字符串分区值、读回时被推断成字符串或 int 不稳定。
    统一在这里按 catalog 的类型声明做一次转换，避免 "int vs str" 导致的空结果。
    """
    table = get_table(table_name)
    out: list[FilterTuple] = []
    for col, op, value in _norm_filters(filters):
        is_partition = (col == table.partition_col)
        dtype = "string" if is_partition else table.columns.get(col)
        if dtype == "int" and not is_partition:
            def _to_int(v):
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return v

            value = [_to_int(v) for v in value] if isinstance(value, list) else _to_int(value)
        elif dtype == "string":
            value = [str(v) for v in value] if isinstance(value, list) else str(value)
        out.append((col, op, value))
    return out


def _empty_frame(table: TableDef) -> pd.DataFrame:
    cols = list(table.columns)
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


def _coerce(df: pd.DataFrame, table: TableDef, partition_col: str | None = None,
            partition_value: str | None = None) -> pd.DataFrame:
    """按表契约补列、定序、转型，保证写入后 schema 稳定。"""
    df = df.copy()
    for col, dtype in table.columns.items():
        if col not in df.columns:
            df[col] = pd.NA
    order = list(table.columns)
    if partition_col and partition_col not in order:
        order.append(partition_col)
    order = [c for c in order if c in df.columns]
    df = df[order]

    for col, dtype in table.columns.items():
        if col not in df.columns:
            continue
        if dtype == "string":
            df[col] = df[col].astype("string")
        elif dtype == "int":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("Int64")
        elif dtype == "float":
            df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
        elif dtype == "bool":
            df[col] = df[col].astype("boolean")

    if partition_col and partition_value is not None:
        df[partition_col] = str(partition_value)
    return df


def to_records(df: pd.DataFrame, max_rows: int | None = None) -> list[dict]:
    """DataFrame → JSON 友好结构（NaN/NaT → None，numpy 标量 → Python 原生）。"""
    if df is None or len(df) == 0:
        return []
    if max_rows is not None and len(df) > max_rows:
        df = df.head(max_rows)
    df = df.astype(object).where(pd.notna(df), None)
    records: list[dict] = []
    for row in df.to_dict(orient="records"):
        clean = {}
        for key, value in row.items():
            if value is None:
                clean[key] = None
            elif hasattr(value, "item"):  # numpy 标量
                clean[key] = value.item()
            elif isinstance(value, (pd.Timestamp,)):
                clean[key] = value.strftime("%Y-%m-%d %H:%M:%S")
            else:
                clean[key] = value
        records.append(clean)
    return records


# ==================================================================== 基类
class BaseEngine:
    name = "base"

    # ---------------- 路径 ----------------
    @staticmethod
    def table_dir(table_name: str) -> Path:
        table = get_table(table_name)
        return Path(Config.WAREHOUSE_ROOT) / table.layer / table.name

    def exists(self, table_name: str) -> bool:
        path = self.table_dir(table_name)
        return path.exists() and any(path.rglob("*.parquet"))

    def require(self, table_name: str) -> Path:
        if not self.exists(table_name):
            raise ApiError(
                ErrCode.DATA_NOT_READY,
                f"数仓表 {table_name} 尚无数据，请先执行：flask etl --table {table_name}",
                data={"table": table_name, "missing": True},
            )
        return self.table_dir(table_name)

    # ---------------- 元数据 ----------------
    @staticmethod
    def partitions(table_name: str) -> list[dict]:
        """扫描目录得到分区清单（文件数/字节数/行数）。"""
        table = get_table(table_name)
        root = BaseEngine.table_dir(table_name)
        if not root.exists():
            return []
        part_col = table.partition_col
        buckets: dict[str | None, dict] = {}
        for path in root.rglob("*.parquet"):
            rel = path.parent.relative_to(root)
            value = None
            if part_col:
                value = None
                for part in rel.parts:
                    if part.startswith(f"{part_col}="):
                        value = part.split("=", 1)[1]
                        break
            bucket = buckets.setdefault(value, {"value": value, "files": 0, "bytes": 0, "rows": 0})
            bucket["files"] += 1
            try:
                bucket["bytes"] += path.stat().st_size
            except OSError:
                pass
        out = []
        for value, bucket in buckets.items():
            bucket["rows"] = _parquet_rows(root if value is None else None, table, value)
            bucket["value"] = value or "__default__"
            out.append(bucket)
        out.sort(key=lambda x: x["value"], reverse=True)
        return out

    @staticmethod
    def available_periods(table_name: str, part_col: str | None = None) -> list[str]:
        table = get_table(table_name)
        part_col = part_col or table.partition_col
        if not part_col:
            return []
        root = BaseEngine.table_dir(table_name)
        if not root.exists():
            return []
        values = []
        for child in root.iterdir():
            if child.is_dir() and child.name.startswith(f"{part_col}="):
                values.append(child.name.split("=", 1)[1])
        return sorted(values)

    @staticmethod
    def latest_period(table_name: str, part_col: str | None = None) -> str | None:
        periods = BaseEngine.available_periods(table_name, part_col)
        return periods[-1] if periods else None

    # ---------------- 抽象 ----------------
    def read(self, table_name: str, columns: Sequence[str] | None = None,
             filters: Iterable[FilterTuple] | None = None, limit: int | None = None) -> pd.DataFrame:
        raise NotImplementedError

    def write(self, table_name: str, df: pd.DataFrame, partition_value: str | None = None,
              mode: str = "overwrite") -> dict:
        raise NotImplementedError

    def drop_partition(self, table_name: str, partition_value: str) -> bool:
        table = get_table(table_name)
        if not table.partition_col:
            return False
        path = _partition_path(self.table_dir(table_name), table.partition_col, partition_value)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            return True
        return False


def _parquet_rows(_root, table: TableDef, value: str | None) -> int:
    """读取 Parquet 元数据计行数（不加载数据）。"""
    try:
        import pyarrow.parquet as pq

        root = BaseEngine.table_dir(table.name)
        target = root if value is None else root / f"{table.partition_col}={value}"
        if not target.exists():
            return 0
        total = 0
        for path in target.rglob("*.parquet"):
            try:
                total += pq.ParquetFile(path).metadata.num_rows
            except Exception:
                continue
        return total
    except Exception:
        return 0


# ==================================================================== pandas
class PandasEngine(BaseEngine):
    name = "pandas"

    @_warehouse_locked
    def read(self, table_name: str, columns=None, filters=None, limit=None) -> pd.DataFrame:
        table = get_table(table_name)
        root = self.require(table_name)
        filters = prepare_filters(table_name, filters)
        try:
            import pyarrow as pa
            import pyarrow.dataset as ds

            # 显式声明分区列类型为 string：否则 pyarrow 会把 year=2015 推断成 int32，
            # 与字符串过滤器冲突（"equal has no kernel matching (int32, string)"）。
            partitioning = "hive"
            if table.partition_col:
                partitioning = ds.partitioning(
                    pa.schema([pa.field(table.partition_col, pa.string())]), flavor="hive")
            dataset = ds.dataset(str(root), format="parquet", partitioning=partitioning)
            expr = None
            for col, op, value in filters:
                field = ds.field(col)
                if op in ("=", "=="):
                    cond = field == value
                elif op == "!=":
                    cond = field != value
                elif op == ">":
                    cond = field > value
                elif op == ">=":
                    cond = field >= value
                elif op == "<":
                    cond = field < value
                elif op == "<=":
                    cond = field <= value
                elif op == "in":
                    cond = field.isin(list(value))
                elif op == "not in":
                    cond = ~field.isin(list(value))
                else:
                    continue
                expr = cond if expr is None else (expr & cond)
            if expr is not None:
                dataset = dataset.filter(expr)
            wanted = [c for c in (columns or []) if c in set(dataset.schema.names)] or None
            arrow_table = dataset.head(limit, columns=wanted) if limit is not None else dataset.to_table(columns=wanted)
            df = arrow_table.to_pandas()
        except ApiError:
            raise
        except Exception as exc:  # 过滤器引用了不存在的列等
            log.warning("pyarrow 读取失败（%s），回退到整表读取 + pandas 过滤", exc)
            df = pd.read_parquet(root)
            for col, op, value in filters:
                if col not in df.columns:
                    continue
                if col == table.partition_col:
                    df[col] = df[col].astype(str)
                    value = [str(v) for v in value] if isinstance(value, list) else str(value)
                if op in ("=", "=="):
                    df = df[df[col] == value]
                elif op == "!=":
                    df = df[df[col] != value]
                elif op == ">":
                    df = df[df[col] > value]
                elif op == ">=":
                    df = df[df[col] >= value]
                elif op == "<":
                    df = df[df[col] < value]
                elif op == "<=":
                    df = df[df[col] <= value]
                elif op == "in":
                    df = df[df[col].isin(list(value))]
                elif op == "not in":
                    df = df[~df[col].isin(list(value))]
            if columns:
                keep = [c for c in columns if c in df.columns]
                df = df[keep]
        if limit is not None:
            df = df.head(limit)
        return df.reset_index(drop=True)

    @_warehouse_locked
    def write(self, table_name: str, df: pd.DataFrame, partition_value: str | None = None,
              mode: str = "overwrite", target_files: int = 8) -> dict:
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = get_table(table_name)
        if mode not in {"overwrite", "append"}:
            raise ValueError("写入模式只能是 overwrite 或 append")
        df = _coerce(df, table, table.partition_col, partition_value)
        if len(df) == 0:
            log.warning("表 %s 写入 0 行，跳过（分区 %s）", table_name, partition_value)
            return {"table": table_name, "rows": 0, "partition": partition_value, "files": 0}

        # Hive 约定：分区列只体现在目录名上，不写进文件。
        # 否则读回时 pyarrow 会同时看到"物理字符串列"和"路径推断的整型分区列"，
        # 报 "Field year has incompatible types: string vs dictionary<int32>"。
        if table.partition_col and partition_value is not None and table.partition_col in df.columns:
            df = df.drop(columns=[table.partition_col])

        root = self.table_dir(table_name)
        root.mkdir(parents=True, exist_ok=True)

        # 目标目录：分区表写到 {col}={value}/，维表直接写表根目录
        if table.partition_col and partition_value is not None:
            target_dir = _partition_path(root, table.partition_col, partition_value)
        else:
            target_dir = root

        # 按目标文件数切块，逐块写 —— 显式控制单文件大小（§4.3 小文件治理）
        n_files = max(1, min(target_files, len(df)))
        bounds = np.linspace(0, len(df), n_files + 1).astype(int)
        written = 0
        staging = Path(tempfile.mkdtemp(prefix=f".{target_dir.name}-", dir=target_dir.parent))
        backup = target_dir.with_name(f".{target_dir.name}-backup-{uuid.uuid4().hex}")
        try:
            for i in range(n_files):
                chunk = df.iloc[bounds[i]:bounds[i + 1]]
                if len(chunk) == 0:
                    continue
                path = staging / f"part-{uuid.uuid4().hex}.parquet"
                pq.write_table(pa.Table.from_pandas(chunk, preserve_index=False), path,
                               compression="snappy")
                written += 1
            if mode == "overwrite":
                if target_dir.exists():
                    os.replace(target_dir, backup)
                try:
                    os.replace(staging, target_dir)
                except Exception:
                    if backup.exists():
                        os.replace(backup, target_dir)
                    raise
            else:
                target_dir.mkdir(parents=True, exist_ok=True)
                for part in staging.glob("*.parquet"):
                    os.replace(part, target_dir / part.name)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            if target_dir.exists():
                shutil.rmtree(backup, ignore_errors=True)
        log.info("写入 %s：%d 行 → %d 个文件，分区=%s", table_name, len(df), written, partition_value)
        return {"table": table_name, "rows": int(len(df)), "partition": partition_value,
                "files": written, "engine": self.name}


# ==================================================================== spark
class SparkEngine(BaseEngine):
    name = "spark"

    def __init__(self):
        from app.services.spark_session import build_spark

        self.spark = build_spark()

    def read(self, table_name: str, columns=None, filters=None, limit=None) -> pd.DataFrame:
        from pyspark.sql import functions as F

        root = self.require(table_name)
        sdf = self.spark.read.parquet(str(root))
        for col, op, value in prepare_filters(table_name, filters):
            if col not in sdf.columns:
                continue
            field = F.col(col)
            if op in ("=", "=="):
                sdf = sdf.filter(field == F.lit(value))
            elif op == "!=":
                sdf = sdf.filter(field != F.lit(value))
            elif op == ">":
                sdf = sdf.filter(field > F.lit(value))
            elif op == ">=":
                sdf = sdf.filter(field >= F.lit(value))
            elif op == "<":
                sdf = sdf.filter(field < F.lit(value))
            elif op == "<=":
                sdf = sdf.filter(field <= F.lit(value))
            elif op == "in":
                sdf = sdf.filter(field.isin(list(value)))
            elif op == "not in":
                sdf = sdf.filter(~field.isin(list(value)))
        if columns:
            keep = [c for c in columns if c in sdf.columns]
            if keep:
                sdf = sdf.select(*keep)
        if limit is not None:
            sdf = sdf.limit(limit)
        return sdf.toPandas()

    def write(self, table_name: str, df: pd.DataFrame, partition_value: str | None = None,
              mode: str = "overwrite", target_files: int = 8) -> dict:
        table = get_table(table_name)
        df = _coerce(df, table, table.partition_col, partition_value)
        if len(df) == 0:
            return {"table": table_name, "rows": 0, "partition": partition_value, "files": 0}
        root = self.table_dir(table_name)
        sdf = self.spark.createDataFrame(df.astype(object).where(pd.notna(df), None))
        writer = sdf.repartition(target_files).write.mode(mode).option("compression", "snappy")
        if table.partition_col and partition_value is not None:
            # 直接覆盖目标分区，避免 Spark 静态 overwrite 删除整张表的其他月份。
            sdf = sdf.drop(table.partition_col)
            writer = sdf.repartition(target_files).write.mode(mode).option("compression", "snappy")
            root = _partition_path(root, table.partition_col, partition_value)
        writer.parquet(str(root))
        log.info("Spark 写入 %s：%d 行，分区=%s", table_name, len(df), partition_value)
        return {"table": table_name, "rows": int(len(df)), "partition": partition_value,
                "files": target_files, "engine": self.name}


# ==================================================================== 工厂
_ENGINE: BaseEngine | None = None


def get_engine(force: str | None = None) -> BaseEngine:
    """按配置选择引擎：auto 时有 Spark 用 Spark，否则 pandas。"""
    global _ENGINE
    wanted = (force or Config.QUERY_ENGINE or "auto").lower()
    if wanted == "spark":
        if _ENGINE is None or not isinstance(_ENGINE, SparkEngine):
            _ENGINE = SparkEngine()
        return _ENGINE
    if wanted == "pandas":
        if _ENGINE is None or not isinstance(_ENGINE, PandasEngine):
            _ENGINE = PandasEngine()
        return _ENGINE

    # auto
    if isinstance(_ENGINE, (PandasEngine, SparkEngine)):
        return _ENGINE
    try:
        from app.services.spark_session import spark_available

        ok, reason = spark_available()
        if ok:
            _ENGINE = SparkEngine()
            log.info("查询引擎：Spark（%s）", reason)
        else:
            _ENGINE = PandasEngine()
            log.info("查询引擎：pandas（%s）", reason)
    except Exception as exc:
        log.info("查询引擎：pandas（Spark 初始化失败：%s）", exc)
        _ENGINE = PandasEngine()
    return _ENGINE


def read(table_name: str, columns=None, filters=None, limit=None) -> pd.DataFrame:
    return get_engine().read(table_name, columns, filters, limit)


def write(table_name: str, df: pd.DataFrame, partition_value: str | None = None,
          mode: str = "overwrite", target_files: int = 8) -> dict:
    return get_engine().write(table_name, df, partition_value, mode, target_files)


def table_overview() -> list[dict]:
    """表浏览接口用：表名、层、粒度、分区、行数、字节数、是否就绪。"""
    engine = get_engine()
    out = []
    for name, table in TABLES.items():
        ready = engine.exists(name)
        parts = engine.partitions(name) if ready else []
        out.append({
            "table": name,
            "layer": table.layer,
            "grain": table.grain,
            "primary_key": table.primary_key,
            "partition_col": table.partition_col,
            "columns": len(table.columns),
            "row_count": sum(p["rows"] for p in parts),
            "bytes": sum(p["bytes"] for p in parts),
            "partitions": len(parts),
            "ready": ready,
            "est_rows": table.est_rows,
            "est_size": table.est_size,
            "description": table.description,
            "tags": list(table.tags),
        })
    return out

