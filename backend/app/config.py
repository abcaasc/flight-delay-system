"""全局配置。

所有阈值、路径、口径均外置，禁止在业务代码里硬编码（对应设计说明书 §八 工程规范）。
生产环境用 .env 覆盖，开发环境给一套可直接跑通的默认值。
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env 优先从项目根加载
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    return int(raw) if raw else default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _path(name: str, default: str) -> Path:
    raw = _env(name, default)
    p = Path(raw)
    if not p.is_absolute():
        p = (BASE_DIR / p).resolve()
    return p


class Config:
    # ---------------- 基础 ----------------
    ENV = _env("FLASK_ENV", "dev")
    SECRET_KEY = _env("SECRET_KEY", "flight-delay-dev-secret-key-please-change-in-prod")
    JSON_AS_ASCII = False
    JSON_SORT_KEYS = False

    # ---------------- 数据源（9 个数据集）----------------
    DATA_ROOT = _path("DATA_ROOT", "../../航班延误预测数据源")

    # ---------------- 数仓 / 临时区 / 模型 ----------------
    WAREHOUSE_ROOT = _path("WAREHOUSE_ROOT", "data/warehouse")
    STAGING_ROOT = _path("STAGING_ROOT", "data/staging")
    MODEL_ROOT = _path("MODEL_ROOT", "data/models")

    # ---------------- 前端静态资源（单容器部署时由后端托管）----------------
    # 指向 frontend 的构建产物（dist）；文件存在时 `/` 直接返回 Vue 应用，
    # 不存在时 `/` 退回 JSON 模块清单，本地开发用 Vite 时互不影响。
    FRONTEND_DIST = _path("FRONTEND_DIST", "../frontend/dist")

    # ---------------- 计算引擎 ----------------
    QUERY_ENGINE = _env("QUERY_ENGINE", "auto")  # auto | pandas | spark
    SPARK_MASTER = _env("SPARK_MASTER", "local[6]")
    SPARK_DRIVER_MEMORY = _env("SPARK_DRIVER_MEMORY", "2g")
    SPARK_EXECUTOR_MEMORY = _env("SPARK_EXECUTOR_MEMORY", "4g")
    SPARK_SQL_SHUFFLE_PARTITIONS = _env_int("SPARK_SQL_SHUFFLE_PARTITIONS", 64)
    SPARK_BROADCAST_THRESHOLD = 31457280  # 30MB（维表广播）
    SPARK_MAX_PARTITION_BYTES = 134217728  # 128MB

    # ---------------- 数据库 ----------------
    DATABASE_URL = _env("DATABASE_URL") or f"sqlite:///{(BASE_DIR / 'instance' / 'flight_delay.db').as_posix()}"

    # ---------------- 缓存 ----------------
    REDIS_URL = _env("REDIS_URL")
    CACHE_TTL_DASHBOARD = _env_int("CACHE_TTL_DASHBOARD", 600)
    CACHE_TTL_QUERY = _env_int("CACHE_TTL_QUERY", 300)

    # ---------------- 认证 ----------------
    JWT_SECRET = _env("JWT_SECRET") or SECRET_KEY
    JWT_ALGORITHM = "HS256"
    JWT_TTL_HOURS = _env_int("JWT_TTL_HOURS", 8)
    JWT_ISSUER = _env("JWT_ISSUER", "flight-delay-system")

    # ---------------- 指标体系口径（唯一真源）----------------
    ONTIME_THRESHOLD_MIN = _env_int("ONTIME_THRESHOLD_MIN", 15)
    SEVERE_DELAY_MIN = _env_int("SEVERE_DELAY_MIN", 60)
    DELAY_MIN_BOUND = _env_int("DELAY_MIN_BOUND", -300)
    DELAY_MAX_BOUND = _env_int("DELAY_MAX_BOUND", 1500)

    # 延误原因分解字段：只做统计展示，禁止进特征（标签泄漏，R-05 / S-05）
    DELAY_REASON_COLUMNS = (
        "carrier_delay",
        "weather_delay",
        "nas_delay",
        "security_delay",
        "late_aircraft_delay",
    )
    # 特征黑名单 = 原因分解 + 事后字段（特征白名单比对用，A-8 泄漏检查）
    FEATURE_BLACKLIST = DELAY_REASON_COLUMNS + (
        "dep_delay",
        "dep_delay_minutes",
        "dep_del15",
        "arr_delay",
        "arr_delay_minutes",
        "arr_del15",
        "actual_elapsed_time",
        "air_time",
        "taxi_in",
        "wheels_off",
        "wheels_on",
        "cancelled",
        "diverted",
    )

    # ---------------- 接入约束 ----------------
    STAGING_MAX_BYTES = _env_int("STAGING_MAX_BYTES", 50 * 1024 * 1024)
    ZIP_MAX_INFLATE_BYTES = _env_int("ZIP_MAX_INFLATE_BYTES", 2 * 1024 * 1024 * 1024)
    ZIP_MAX_ENTRY_BYTES = _env_int("ZIP_MAX_ENTRY_BYTES", 500 * 1024 * 1024)
    ALLOWED_EXTENSIONS = {".csv", ".zip", ".dat", ".npy", ".json", ".xlsx"}

    # ---------------- 分页与导出 ----------------
    MAX_PAGE_SIZE = _env_int("MAX_PAGE_SIZE", 200)
    DEFAULT_PAGE_SIZE = 20
    EXPORT_MAX_ROWS = _env_int("EXPORT_MAX_ROWS", 1000)

    # ---------------- 可复现 ----------------
    RANDOM_SEED = _env_int("RANDOM_SEED", 42)
    TRAIN_VALID_TEST_RATIO = (0.70, 0.15, 0.15)

    # ---------------- 业务规则 ----------------
    BTS_DATE_RANGE = ("2015-01-01", "2024-12-31")  # 字段级闸门
    BTS_MONTH_COUNT = 120
    CHINA_SLOT_MINUTES = 30  # STPN 机场×30 分钟时间片
    WEATHER_WINDOW_MIN = (60, 180)  # 起飞前 1–3 小时

    # ---------------- AI 助手 ----------------
    DEEPSEEK_API_KEY = _env("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    DEEPSEEK_MODEL = _env("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_TIMEOUT = _env_int("DEEPSEEK_TIMEOUT", 60)

    # ---------------- 日志 ----------------
    LOG_LEVEL = _env("LOG_LEVEL", "INFO")


def ensure_dirs() -> None:
    """确保运行期目录存在（幂等）。"""
    for p in (Config.WAREHOUSE_ROOT, Config.STAGING_ROOT, Config.MODEL_ROOT, BASE_DIR / "instance"):
        Path(p).mkdir(parents=True, exist_ok=True)


CONFIG_MAP = {"dev": Config, "prod": Config, "test": Config}

