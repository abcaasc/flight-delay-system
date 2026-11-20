"""跨进程保存作业心跳，恢复意外中断后的作业状态。"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, exists, or_, update
from sqlalchemy.orm import Session

from app.extensions import db
from app.models import JobLease, JobRun
from app.utils.logger import get_logger

HEARTBEAT_SECONDS = 30
LEASE_SECONDS = 180
log = get_logger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JobHeartbeat:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self._stopped = threading.Event()

    def register(self, request: dict | None = None) -> None:
        db.session.add(JobLease(job_id=self.job_id, heartbeat_at=_utc_now(),
                                request_json=json.dumps(request, ensure_ascii=False) if request else ""))

    def start(self) -> None:
        threading.Thread(target=self._run, name=f"heartbeat-{self.job_id}", daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()

    def _run(self) -> None:
        while not self._stopped.wait(HEARTBEAT_SECONDS):
            try:
                with Session(db.engine) as session:
                    lease = session.get(JobLease, self.job_id)
                    if lease is None:
                        return
                    lease.heartbeat_at = _utc_now()
                    session.commit()
            except Exception:
                log.warning("作业 %s 心跳保存失败", self.job_id, exc_info=True)


def reconcile_jobs() -> int:
    local_now = datetime.now()
    legacy_cutoff = (local_now - timedelta(seconds=LEASE_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")
    lease = exists().where(JobLease.job_id == JobRun.job_id)
    fresh_lease = exists().where(JobLease.job_id == JobRun.job_id,
                                 JobLease.heartbeat_at > _utc_now() - timedelta(seconds=LEASE_SECONDS))
    with Session(db.engine) as session:
        result = session.execute(
            update(JobRun)
            .where(JobRun.status.in_(("queued", "running")), ~fresh_lease,
                   or_(lease, JobRun.started_at <= legacy_cutoff))
            .values(status="interrupted", finished_at=local_now.strftime("%Y-%m-%d %H:%M:%S"),
                    err_code="E_JOB_INTERRUPTED", err_msg="作业心跳已过期，可重新提交作业"))
        session.commit()
        return result.rowcount
