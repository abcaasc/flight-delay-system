"""ETL 作业公共设施：作业上下文、剔除原因计数、审计落库、数值转换。"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from app.extensions import db
from app.models import FileManifest, IngestAudit, IngestFailed, JobRun
from app.services.job_runtime import JobHeartbeat
from app.utils.logger import JobLogger, get_logger

log = get_logger(__name__)

# 剔除/异常原因码（与设计说明书 §5.1 清洗决策流程一致）
DROP_REASONS = {
    "E01": "关键时间字段缺失（R-07）",
    "E02": "HHMM 非法脏值（R-02）",
    "E03": "起降机场代码非法",
    "E04": "日期越界（不在 2015-01-01~2024-12-31）",
    "E05": "重复记录（R-06）",
    "E06": "取消航班（不进分类训练集，R-04）",
    "E07": "延误值超出值域（标记 outlier）",
    "E08": "气象站缺失（weather_available=0）",
}


class DropCounter:
    """剔除/标记原因计数：写入作业日志与审计字段，保证"全部计数留痕"。

    区分两个口径（避免把"标记"算成"剔除"）：
        drops  —— 真正被剔除的行（E01~E05）
        flags  —— 保留但打标/另类的行（E06 取消、E07 越界、E08 无气象）
    """

    DROP_CODES = ("E01", "E02", "E03", "E04", "E05")

    def __init__(self):
        self._drops: dict[str, int] = {}
        self._flags: dict[str, int] = {}

    def add(self, code: str, n: int = 1) -> None:
        """记一次剔除。"""
        if n:
            self._drops[code] = self._drops.get(code, 0) + int(n)

    def flag(self, code: str, n: int = 1) -> None:
        """记一次标记（不剔除）。"""
        if n:
            self._flags[code] = self._flags.get(code, 0) + int(n)

    @property
    def total(self) -> int:
        """剔除行数（不含标记）。"""
        return sum(self._drops.values())

    @property
    def flagged(self) -> int:
        return sum(self._flags.values())

    @staticmethod
    def _label(codes: dict[str, int]) -> dict:
        return {f"{k}({DROP_REASONS.get(k, '')})": v for k, v in sorted(codes.items())}

    def as_dict(self) -> dict:
        return self._label(self._drops)

    def flags_dict(self) -> dict:
        return self._label(self._flags)

    def as_json(self) -> str:
        return json.dumps({"drops": self._drops, "flags": self._flags}, ensure_ascii=False)


def new_job_id(prefix: str = "job") -> str:
    return f"{prefix}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"


@dataclass
class JobContext:
    """一次作业的上下文：进度、阶段、剔除统计，并且逐阶段落库便于页面轮询。"""

    job_name: str
    dataset: str = ""
    period: str = ""
    mode: str = "incremental"
    engine: str = "pandas"
    operator: str = "system"
    job_id: str = field(default_factory=lambda: new_job_id("job"))
    rows_in: int = 0
    rows_out: int = 0
    total_files: int = 0
    done_files: int = 0
    current_file: str = ""
    stages: list = field(default_factory=list)
    drops: DropCounter = field(default_factory=DropCounter)
    _t0: float = field(default_factory=time.time)
    _stage_t0: float = field(default_factory=time.time)
    _logger: JobLogger | None = None
    _heartbeat: JobHeartbeat | None = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._logger = JobLogger(self.job_name, self.job_id)
        self._logger.start(dataset=self.dataset or "-", period=self.period or "-",
                           mode=self.mode, engine=self.engine, operator=self.operator)

    # ---------------- 日志 ----------------
    def info(self, msg: str, *args) -> None:
        self._logger.info(msg, *args)

    def warn(self, msg: str, *args) -> None:
        self._logger.warn(msg, *args)

    def error(self, msg: str, *args) -> None:
        self._logger.error(msg, *args)

    # ---------------- 阶段 ----------------
    def stage_start(self, name: str) -> None:
        self._stage_t0 = time.time()
        self.info("阶段开始：%s", name)

    def stage_end(self, name: str, note: str = "") -> None:
        elapsed = round(time.time() - self._stage_t0, 2)
        self.stages.append({"stage": name, "elapsed_sec": elapsed, "note": note})
        self.info("阶段结束：%s（%.2fs）%s", name, elapsed, note)

    # ---------------- 进度 ----------------
    def set_progress(self, done_files: int, total_files: int, current_file: str = "") -> None:
        self.done_files = done_files
        self.total_files = total_files
        self.current_file = current_file

    def progress_pct(self) -> float:
        if not self.total_files:
            return 0.0
        return min(99.9, round(self.done_files / self.total_files * 100, 1))

    # ---------------- 落库 ----------------
    def persist(self, status: str | None = None, err_code: str = "", err_msg: str = "") -> None:
        """把当前状态写入 sys_job_run（页面每 2 秒轮询看到的就是这条记录）。"""
        heartbeat = None
        try:
            row = db.session.query(JobRun).filter_by(job_id=self.job_id).first()
            if row is None:
                row = JobRun(job_id=self.job_id, job_name=self.job_name,
                             started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                db.session.add(row)
                if status == "running":
                    heartbeat = JobHeartbeat(self.job_id)
                    heartbeat.register()
            row.dataset = self.dataset
            row.period = self.period
            row.mode = self.mode
            row.engine = self.engine
            row.operator = self.operator
            if status:
                row.status = status
            row.progress = 100.0 if status == "success" else self.progress_pct()
            row.current_file = self.current_file[:256]
            row.total_files = self.total_files
            row.done_files = self.done_files
            row.rows_in = self.rows_in
            row.rows_out = self.rows_out
            row.drop_rows = self.drops.total
            row.drop_reason = self.drops.as_json()
            row.stages = json.dumps(self.stages, ensure_ascii=False)
            row.err_code = err_code
            row.err_msg = err_msg[:2000]
            row.elapsed_sec = round(time.time() - self._t0, 1)
            if status in {"success", "failed", "blocked", "cancelled"}:
                row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.session.commit()
            if heartbeat is not None:
                heartbeat.start()
                self._heartbeat = heartbeat
        except Exception as exc:  # 落库失败不能打断数据流程
            db.session.rollback()
            log.warning("作业 %s 状态落库失败：%s", self.job_id, exc)

    def finish(self, status: str = "success", err_code: str = "", err_msg: str = "") -> dict:
        elapsed = time.time() - self._t0
        self._logger.finish(self.rows_in, self.rows_out, elapsed, self.drops.as_dict())
        self.persist(status=status, err_code=err_code, err_msg=err_msg)
        if self._heartbeat is not None:
            self._heartbeat.stop()
        return {
            "job_id": self.job_id,
            "job_name": self.job_name,
            "dataset": self.dataset,
            "period": self.period,
            "status": status,
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "drop_rows": self.drops.total,
            "drop_reason": self.drops.as_dict(),
            "flag_reason": self.drops.flags_dict(),
            "elapsed_sec": round(elapsed, 2),
            "stages": self.stages,
            "err_code": err_code,
            "err_msg": err_msg,
        }


# ---------------------------------------------------------------- 审计与清单
def record_audit(ctx: JobContext, file_name: str, period: str, file_md5: str, file_bytes: int,
                 raw_rows: int, ods_rows: int, status: str, elapsed_sec: float,
                 err_code: str = "", channel: str = "A") -> None:
    try:
        row = IngestAudit(
            job_id=ctx.job_id, dataset=ctx.dataset, period=period, file_name=file_name[:256],
            file_md5=file_md5 or "", file_bytes=int(file_bytes or 0),
            raw_rows=int(raw_rows or 0), ods_rows=int(ods_rows or 0),
            drop_rows=int(max(0, (raw_rows or 0) - (ods_rows or 0))),
            drop_reason=ctx.drops.as_json()[:256], channel=channel, mode=ctx.mode,
            status=status, err_code=err_code, operator=ctx.operator,
            started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            elapsed_sec=round(elapsed_sec, 2),
        )
        db.session.add(row)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning("写接入审计失败：%s", exc)


def record_failure(ctx: JobContext, file_name: str, period: str, err_code: str, err_msg: str) -> None:
    """写死信表（3 次重试仍失败 → 人工介入）。"""
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = (db.session.query(IngestFailed)
               .filter_by(dataset=ctx.dataset, period=period, file_name=file_name[:256]).first())
        if row is None:
            row = IngestFailed(job_id=ctx.job_id, dataset=ctx.dataset, period=period,
                               file_name=file_name[:256], first_fail_at=now)
            db.session.add(row)
        row.err_code = err_code
        row.err_msg = err_msg[:2000]
        row.attempts = int(row.attempts or 0) + 1
        row.last_fail_at = now
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning("写死信失败：%s", exc)


def upsert_manifest(dataset: str, period: str, file_name: str, rel_path: str,
                    file_md5: str, file_bytes: int, row_count: int) -> None:
    try:
        row = (db.session.query(FileManifest)
               .filter_by(dataset=dataset, period=period, file_name=file_name[:256]).first())
        if row is None:
            row = FileManifest(dataset=dataset, period=period, file_name=file_name[:256])
            db.session.add(row)
        row.rel_path = rel_path[:512]
        row.file_md5 = file_md5 or ""
        row.file_bytes = int(file_bytes or 0)
        row.row_count = int(row_count or 0)
        row.verified = 1
        row.verified_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning("写文件清单失败：%s", exc)


# ---------------------------------------------------------------- 数值转换
def to_num(series: pd.Series, lower: float | None = None,
           upper: float | None = None) -> pd.Series:
    """字符串列 → 数值列；超值域的置 NaN 并交给调用方统计。"""
    out = pd.to_numeric(series.astype("string").str.replace(",", "", regex=False),
                        errors="coerce")
    if lower is not None:
        out = out.where(out.isna() | (out >= lower))
    if upper is not None:
        out = out.where(out.isna() | (out <= upper))
    return out.astype("float64")


def to_clock_minutes(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    """HHMM 字符串列 → (当日分钟数, 状态列)。

    向量化实现 R-02：2400 折算 1440，时≥24 或分≥60 之外的脏值置 NaN。
    """
    text = series.astype("string").str.strip()
    text = text.str.replace(r"\.0$", "", regex=True)
    minutes = pd.Series(np.nan, index=series.index, dtype="float64")
    status = pd.Series("invalid", index=series.index, dtype="object")
    status.loc[text.isna() | text.eq("")] = "empty"
    text = text.where(text.str.fullmatch(r"\d{1,4}", na=False))

    digits = text.dropna()
    if len(digits):
        padded = digits.str.zfill(4)
        hour = pd.to_numeric(padded.str[:2], errors="coerce")
        minute = pd.to_numeric(padded.str[2:], errors="coerce")
        valid = hour.notna() & minute.notna() & (minute < 60) & (hour < 24)
        fixed = (hour == 24) & (minute == 0)
        values = hour * 60 + minute
        minutes.loc[valid[valid].index] = values[valid]
        minutes.loc[fixed[fixed].index] = 1440
        status.loc[valid[valid].index] = "ok"
        status.loc[fixed[fixed].index] = "fixed_2400"
        status.loc[digits.index.difference(valid[valid].index.union(fixed[fixed].index))] = "invalid"

    return minutes, status

