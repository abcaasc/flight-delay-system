"""ORM 模型：MySQL 8.0 业务库（账户、作业、审计、清单、模型版本）。

数仓（ODS/DWD/DWS/ADS 的 Parquet 表）不放这里，由 Spark 管理；
MySQL 只存"系统业务数据与账户信息"（开题报告 §系统运行环境）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    return datetime.now()


# ============================================================ 账户与权限
class User(Base):
    __tablename__ = "sys_user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, comment="登录名")
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False, comment="pbkdf2 加盐哈希")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="guest",
                                      comment="guest/ops/analyst/admin")
    display_name: Mapped[str] = mapped_column(String(64), default="", comment="显示名")
    email: Mapped[str] = mapped_column(String(128), default="")
    scope_airports: Mapped[str] = mapped_column(String(512), default="",
                                                comment="数据范围预留字段，当前不启用")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def to_dict(self, with_sensitive: bool = False) -> dict:
        data = {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "email": self.email,
            "scope_airports": self.scope_airports,
            "is_active": bool(self.is_active),
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
            "last_login_at": self.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_login_at else None,
        }
        if with_sensitive:
            data["password_hash"] = self.password_hash
        return data


# ============================================================ 审计
class AuditLog(Base):
    __tablename__ = "sys_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), index=True, comment="yyyy-MM-dd HH:mm:ss")
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16), default="")
    action: Mapped[str] = mapped_column(String(64), index=True, comment="login/ingest/model_online/...")
    resource: Mapped[str] = mapped_column(String(128), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    result: Mapped[str] = mapped_column(String(16), default="success")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "username": self.username, "role": self.role,
            "action": self.action, "resource": self.resource, "detail": self.detail,
            "ip": self.ip, "trace_id": self.trace_id, "result": self.result,
        }


# ============================================================ 告警
class AlertRule(Base):
    __tablename__ = "sys_alert_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    metric: Mapped[str] = mapped_column(String(64), nullable=False,
                                        comment="ontime_rate/delay_rate/cancel_rate/avg_delay/rows_dropped")
    comparator: Mapped[str] = mapped_column(String(8), default="lt", comment="lt/gt/le/ge")
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), default="airport", comment="airport/route/global/job")
    scope_value: Mapped[str] = mapped_column(String(128), default="", comment="ATL / ATL-ORD / *")
    level: Mapped[str] = mapped_column(String(16), default="warning", comment="info/warning/critical")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "metric": self.metric,
            "comparator": self.comparator, "threshold": self.threshold,
            "scope_type": self.scope_type, "scope_value": self.scope_value,
            "level": self.level, "enabled": bool(self.enabled),
            "created_by": self.created_by,
            "created_at": self.created_at.strftime("%Y-%m-%d %H:%M:%S") if self.created_at else None,
        }


class AlertEvent(Base):
    __tablename__ = "sys_alert_event"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, index=True)
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    triggered_at: Mapped[str] = mapped_column(String(19), index=True)
    level: Mapped[str] = mapped_column(String(16), default="warning")
    scope: Mapped[str] = mapped_column(String(128), default="")
    observed: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(512), default="")
    acked: Mapped[bool] = mapped_column(Boolean, default=False)
    acked_by: Mapped[str] = mapped_column(String(64), default="")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "rule_id": self.rule_id, "rule_name": self.rule_name,
            "triggered_at": self.triggered_at, "level": self.level, "scope": self.scope,
            "observed": round(self.observed, 4), "message": self.message,
            "acked": bool(self.acked), "acked_by": self.acked_by,
        }


# ============================================================ 作业运行记录
class JobRun(Base):
    __tablename__ = "sys_job_run"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(48), unique=True, nullable=False)
    job_name: Mapped[str] = mapped_column(String(64), index=True, comment="ingest_bts/etl_dwd/...")
    dataset: Mapped[str] = mapped_column(String(32), default="", index=True)
    period: Mapped[str] = mapped_column(String(16), default="", comment="yyyy-MM 或 yyyy；区间用逗号")
    mode: Mapped[str] = mapped_column(String(16), default="incremental", comment="full/incremental")
    engine: Mapped[str] = mapped_column(String(16), default="pandas", comment="spark/pandas")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True,
                                        comment="queued/running/success/failed/blocked/cancelled")
    progress: Mapped[float] = mapped_column(Float, default=0.0, comment="0-100")
    current_file: Mapped[str] = mapped_column(String(256), default="")
    total_files: Mapped[int] = mapped_column(Integer, default=0)
    done_files: Mapped[int] = mapped_column(Integer, default=0)
    rows_in: Mapped[int] = mapped_column(BigInteger, default=0)
    rows_out: Mapped[int] = mapped_column(BigInteger, default=0)
    drop_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    drop_reason: Mapped[str] = mapped_column(Text, default="", comment="JSON: {E01: 123}")
    stages: Mapped[str] = mapped_column(Text, default="", comment="JSON: [{stage,status,elapsed_sec}]")
    err_code: Mapped[str] = mapped_column(String(32), default="")
    err_msg: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[str] = mapped_column(String(19), default="")
    finished_at: Mapped[str] = mapped_column(String(19), default="")
    elapsed_sec: Mapped[float] = mapped_column(Float, default=0.0)

    __table_args__ = (Index("ix_job_dataset_period", "dataset", "period"),)

    def to_dict(self) -> dict:
        import json

        def _load(text: str, fallback):
            try:
                return json.loads(text) if text else fallback
            except Exception:
                return fallback

        return {
            "job_id": self.job_id, "job_name": self.job_name, "dataset": self.dataset,
            "period": self.period, "mode": self.mode, "engine": self.engine,
            "status": self.status, "progress": round(self.progress or 0, 1),
            "current_file": self.current_file,
            "total_files": self.total_files, "done_files": self.done_files,
            "rows_in": self.rows_in, "rows_out": self.rows_out, "drop_rows": self.drop_rows,
            "drop_reason": _load(self.drop_reason, {}),
            "stages": _load(self.stages, []),
            "err_code": self.err_code, "err_msg": self.err_msg,
            "operator": self.operator,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "elapsed_sec": round(self.elapsed_sec or 0, 1),
        }


# ============================================================ 数据接入
class IngestAudit(Base):
    """文件级接入审计（一个文件一行），对应 ods_ingest_audit。"""

    __tablename__ = "ods_ingest_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(48), index=True)
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    file_name: Mapped[str] = mapped_column(String(256))
    file_md5: Mapped[str] = mapped_column(String(32), default="")
    file_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    raw_rows: Mapped[int] = mapped_column(BigInteger, default=0, comment="源文件行数（含表头另计）")
    ods_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    drop_rows: Mapped[int] = mapped_column(BigInteger, default=0)
    drop_reason: Mapped[str] = mapped_column(String(256), default="")
    channel: Mapped[str] = mapped_column(String(4), default="A", comment="A 批处理/B 控制面/C 页面直传")
    mode: Mapped[str] = mapped_column(String(16), default="incremental")
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    err_code: Mapped[str] = mapped_column(String(32), default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    started_at: Mapped[str] = mapped_column(String(19), default="")
    finished_at: Mapped[str] = mapped_column(String(19), default="")
    elapsed_sec: Mapped[float] = mapped_column(Float, default=0.0)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "job_id": self.job_id, "dataset": self.dataset, "period": self.period,
            "file_name": self.file_name, "file_md5": self.file_md5, "file_bytes": self.file_bytes,
            "raw_rows": self.raw_rows, "ods_rows": self.ods_rows, "drop_rows": self.drop_rows,
            "drop_reason": self.drop_reason, "channel": self.channel, "mode": self.mode,
            "status": self.status, "err_code": self.err_code, "operator": self.operator,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "elapsed_sec": round(self.elapsed_sec or 0, 2),
        }


class FileManifest(Base):
    """文件级清单与对账状态（对应 00_清单与字典/manifest.csv）。"""

    __tablename__ = "ods_file_manifest"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    period: Mapped[str] = mapped_column(String(16), index=True)
    file_name: Mapped[str] = mapped_column(String(256))
    rel_path: Mapped[str] = mapped_column(String(512), default="")
    file_md5: Mapped[str] = mapped_column(String(32), default="")
    file_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    row_count: Mapped[int] = mapped_column(BigInteger, default=0)
    verified: Mapped[int] = mapped_column(Integer, default=0, comment="1=已对账")
    verified_at: Mapped[str] = mapped_column(String(19), default="")

    __table_args__ = (UniqueConstraint("dataset", "period", "file_name", name="uq_manifest_key"),)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "dataset": self.dataset, "period": self.period,
            "file_name": self.file_name, "rel_path": self.rel_path,
            "file_md5": self.file_md5, "file_bytes": self.file_bytes,
            "row_count": self.row_count, "verified": int(self.verified or 0),
            "verified_at": self.verified_at,
        }


class StagingFile(Base):
    """页面直传临时区（单文件 ≤50MB，人工确认后 promote）。"""

    __tablename__ = "ods_staging_file"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    staging_id: Mapped[str] = mapped_column(String(48), unique=True)
    dataset: Mapped[str] = mapped_column(String(32), default="custom", index=True)
    file_name: Mapped[str] = mapped_column(String(256))
    file_path: Mapped[str] = mapped_column(String(512))
    file_md5: Mapped[str] = mapped_column(String(32), default="")
    file_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")
    uploaded_at: Mapped[str] = mapped_column(String(19), default="")
    promoted: Mapped[int] = mapped_column(Integer, default=0)
    promoted_at: Mapped[str] = mapped_column(String(19), default="")

    def to_dict(self) -> dict:
        return {
            "staging_id": self.staging_id, "dataset": self.dataset, "file_name": self.file_name,
            "file_path": self.file_path, "file_md5": self.file_md5, "file_bytes": self.file_bytes,
            "uploaded_by": self.uploaded_by, "uploaded_at": self.uploaded_at,
            "promoted": int(self.promoted or 0), "promoted_at": self.promoted_at,
        }


class IngestFailed(Base):
    """死信表：3 次重试仍失败，需人工介入。"""

    __tablename__ = "ods_ingest_failed"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(48), index=True)
    dataset: Mapped[str] = mapped_column(String(32), index=True)
    period: Mapped[str] = mapped_column(String(16), default="")
    file_name: Mapped[str] = mapped_column(String(256), default="")
    err_code: Mapped[str] = mapped_column(String(32), default="")
    err_msg: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    ignored: Mapped[int] = mapped_column(Integer, default=0, comment="1=人工标记忽略")
    first_fail_at: Mapped[str] = mapped_column(String(19), default="")
    last_fail_at: Mapped[str] = mapped_column(String(19), default="")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "job_id": self.job_id, "dataset": self.dataset, "period": self.period,
            "file_name": self.file_name, "err_code": self.err_code, "err_msg": self.err_msg,
            "attempts": self.attempts, "ignored": int(self.ignored or 0),
            "first_fail_at": self.first_fail_at, "last_fail_at": self.last_fail_at,
        }


# ============================================================ 模型中心
class ModelVersion(Base):
    __tablename__ = "model_version"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    model_ver: Mapped[str] = mapped_column(String(64), unique=True, nullable=False,
                                           comment="如 lgbm_cls15_20261117_120000")
    task: Mapped[str] = mapped_column(String(32), index=True,
                                      comment="cls15(二分类)/level3(三级分类)/reg(分钟回归)")
    algo: Mapped[str] = mapped_column(String(32), default="lightgbm")
    feature_set: Mapped[str] = mapped_column(Text, default="", comment="逗号分隔特征名")
    train_range: Mapped[str] = mapped_column(String(32), default="", comment="yyyy-MM~yyyy-MM")
    test_range: Mapped[str] = mapped_column(String(32), default="")
    params_json: Mapped[str] = mapped_column(Text, default="")
    metrics_json: Mapped[str] = mapped_column(Text, default="")
    artifact_path: Mapped[str] = mapped_column(String(512), default="")
    is_online: Mapped[int] = mapped_column(Integer, default=0, index=True)
    seed: Mapped[int] = mapped_column(Integer, default=42)
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(19), default="")

    def to_dict(self) -> dict:
        import json

        def _load(text):
            try:
                return json.loads(text) if text else {}
            except Exception:
                return {}

        return {
            "model_ver": self.model_ver, "task": self.task, "algo": self.algo,
            "feature_set": [f for f in (self.feature_set or "").split(",") if f],
            "train_range": self.train_range, "test_range": self.test_range,
            "params": _load(self.params_json), "metrics": _load(self.metrics_json),
            "artifact_path": self.artifact_path, "is_online": int(self.is_online or 0),
            "seed": self.seed, "created_by": self.created_by, "created_at": self.created_at,
        }


class CaseBookmark(Base):
    """典型延误案例库（收藏/标注）。"""

    __tablename__ = "case_bookmark"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    flight_sk: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(256), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[str] = mapped_column(String(19), default="")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "flight_sk": self.flight_sk, "title": self.title,
            "note": self.note, "tags": [t for t in (self.tags or "").split(",") if t],
            "created_by": self.created_by, "created_at": self.created_at,
        }


class AiChatLog(Base):
    __tablename__ = "ai_chat_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[str] = mapped_column(String(19), index=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    context_json: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    degraded: Mapped[int] = mapped_column(Integer, default=0, comment="1=未配置 API Key 走本地摘要")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "username": self.username,
            "question": self.question, "answer": self.answer,
            "model": self.model, "degraded": int(self.degraded or 0),
            "latency_ms": self.latency_ms,
        }


__all__ = [
    "Base", "User", "AuditLog", "AlertRule", "AlertEvent", "JobRun", "IngestAudit",
    "FileManifest", "StagingFile", "IngestFailed", "ModelVersion", "CaseBookmark", "AiChatLog",
]
