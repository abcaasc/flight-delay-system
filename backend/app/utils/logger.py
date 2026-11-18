"""日志与作业日志规范（对应 §八 工程规范：job_id / 入行数 / 出行数 / 剔除分布 / 耗时）。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-22s | %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"
_configured = False


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger()
    root.setLevel(level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
    root.addHandler(console)

    if log_dir:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(Path(log_dir) / "backend.log", maxBytes=20 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT, _DATE_FMT))
        root.addHandler(fh)

    # 第三方噪声降级
    for noisy in ("urllib3", "werkzeug", "py4j", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


class JobLogger:
    """作业日志：统一输出 job_id / 输入输出行数 / 剔除原因分布 / 耗时。"""

    def __init__(self, job_name: str, job_id: str):
        self.job_name = job_name
        self.job_id = job_id
        self.logger = get_logger(f"job.{job_name}")

    def start(self, **ctx) -> None:
        extra = " ".join(f"{k}={v}" for k, v in ctx.items())
        self.logger.info("[%s] START job_id=%s %s", self.job_name, self.job_id, extra)

    def info(self, msg: str, *args) -> None:
        self.logger.info("[%s] job_id=%s " + msg, self.job_name, self.job_id, *args)

    def warn(self, msg: str, *args) -> None:
        self.logger.warning("[%s] job_id=%s " + msg, self.job_name, self.job_id, *args)

    def error(self, msg: str, *args) -> None:
        self.logger.error("[%s] job_id=%s " + msg, self.job_name, self.job_id, *args)

    def finish(self, rows_in: int, rows_out: int, elapsed_sec: float,
               drop_reason: dict | None = None) -> None:
        self.logger.info(
            "[%s] DONE job_id=%s rows_in=%d rows_out=%d elapsed=%.1fs drop=%s",
            self.job_name, self.job_id, rows_in, rows_out, elapsed_sec, drop_reason or {},
        )
