"""SparkSession 工厂（对应《数据接入设计说明》§4.1 作业参数）。

⚠ 版本约束：PySpark 3.4.x 只支持 JDK 8/11/17，不支持 JDK 21。
本机默认 JAVA_HOME 若指向 JDK 21，请显式指定：
    SET JAVA_HOME=D:\\tools\\jdk-17
或把 JDK 17 的 bin 放在 PATH 前面。检测不到可用 JVM 时，查询引擎自动降级为
pandas（单机多线程模拟分布式），并在 /api/v1/system/health 中如实标注。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache

from app.config import Config
from app.utils.logger import get_logger

log = get_logger(__name__)

_SPARK_SESSION = None


def java_version() -> str | None:
    """探测 java 版本号（不依赖 pyspark）。"""
    java = shutil.which("java")
    if not java:
        return None
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=8)
        text = (out.stderr or out.stdout or "").strip()
        return text.splitlines()[0] if text else None
    except Exception:
        return None


def _java_major() -> int | None:
    text = java_version() or ""
    for marker in ('"', ):
        if marker in text:
            try:
                return int(text.split(marker)[1].split(".")[0])
            except Exception:
                pass
    return None


def spark_available() -> tuple[bool, str]:
    """PySpark 是否可用（包存在 + JVM 版本可接受）。"""
    try:
        import pyspark  # noqa: F401
    except Exception:
        return False, "未安装 pyspark（pip install pyspark==3.4.4）"
    major = _java_major()
    if major is None:
        return False, "未检测到 java，可设置 JAVA_HOME 指向 JDK 11/17"
    if major >= 21:
        return False, f"检测到 JDK {major}，PySpark 3.4 需要 JDK 11/17（请设置 JAVA_HOME）"
    return True, f"PySpark 就绪（JDK {major}）"


def build_spark(app_name: str = "flight-delay-system"):
    """按设计说明书 §4.1 的参数构建 SparkSession。"""
    global _SPARK_SESSION
    if _SPARK_SESSION is not None:
        return _SPARK_SESSION

    ok, reason = spark_available()
    if not ok:
        raise RuntimeError(f"Spark 不可用：{reason}")

    from pyspark.sql import SparkSession

    _SPARK_SESSION = (
        SparkSession.builder.appName(app_name)
        .master(Config.SPARK_MASTER)
        .config("spark.driver.memory", Config.SPARK_DRIVER_MEMORY)
        .config("spark.executor.memory", Config.SPARK_EXECUTOR_MEMORY)
        .config("spark.sql.shuffle.partitions", str(Config.SPARK_SQL_SHUFFLE_PARTITIONS))
        .config("spark.sql.adaptive.enabled", "true")
        .config("spark.sql.adaptive.skewJoin.enabled", "true")
        .config("spark.sql.autoBroadcastJoinThreshold", str(Config.SPARK_BROADCAST_THRESHOLD))
        .config("spark.sql.files.maxPartitionBytes", str(Config.SPARK_MAX_PARTITION_BYTES))
        .config("spark.sql.sources.partitionOverwriteMode", "dynamic")
        .config("spark.sql.parquet.compression.codec", "snappy")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.showConsoleProgress", "false")
        .config("spark.driver.host", "127.0.0.1")
        .getOrCreate()
    )
    _SPARK_SESSION.sparkContext.setLogLevel("WARN")
    log.info("SparkSession 已创建：master=%s", Config.SPARK_MASTER)
    return _SPARK_SESSION


@lru_cache(maxsize=1)
def engine_info() -> dict:
    ok, reason = spark_available()
    return {
        "configured": Config.QUERY_ENGINE,
        "spark_available": ok,
        "spark_reason": reason,
        "java": java_version(),
        "java_home": os.getenv("JAVA_HOME", ""),
        "spark_master": Config.SPARK_MASTER,
    }


def stop_spark() -> None:
    global _SPARK_SESSION
    if _SPARK_SESSION is not None:
        try:
            _SPARK_SESSION.stop()
        except Exception:
            pass
        _SPARK_SESSION = None
