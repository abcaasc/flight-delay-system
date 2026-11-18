"""入湖与 ETL 作业编排（控制面）。

页面只下发指令与进度查询，数据面在本地文件系统 → Spark/pandas → Parquet 上完成，
字节从不经过浏览器（《数据接入设计说明》§一 通道语义）。

单机版并发控制：以 `dataset` 为粒度的进程内锁；同一数据集同时只允许 1 个写作业。
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

from app.config import Config
from app.etl import ads as ads_etl
from app.etl import dim as dim_etl
from app.etl import dwd as dwd_etl
from app.etl import dws as dws_etl
from app.etl import ods as ods_etl
from app.services import datasource, engine, model as model_service
from app.services.cache import invalidate
from app.utils.logger import get_logger
from app.utils.response import ApiError, ErrCode

log = get_logger(__name__)

JOB_TYPES = {
    "ingest": "仅入湖（ODS）",
    "etl": "仅数仓加工（DWD→DWS→ADS）",
    "full": "入湖 + 数仓加工",
    "train": "模型训练与批量预测",
    "dims": "重建维表（机场/航司/日历）",
}

_LOCK = threading.Lock()
_ACTIVE: dict[str, str] = {}      # dataset -> job_id
_THREADS: dict[str, threading.Thread] = {}


def _failures(result, path: str = "") -> list[str]:
    """作业函数既可能抛异常，也可能以嵌套结果报告失败。"""
    if isinstance(result, dict):
        failed = [path or "job"] if result.get("status") == "failed" else []
        for key, value in result.items():
            failed.extend(_failures(value, f"{path}/{key}"))
        return failed
    if isinstance(result, list):
        return [p for i, value in enumerate(result) for p in _failures(value, f"{path}/{i}")]
    return []


def _acquire(dataset: str, job_id: str) -> None:
    with _LOCK:
        current = _ACTIVE.get(dataset)
        if current:
            raise ApiError(ErrCode.PARAM_INVALID,
                           f"数据集 {dataset} 已有作业在运行（job_id={current}），请等待完成")
        _ACTIVE[dataset] = job_id


def _release(dataset: str) -> None:
    with _LOCK:
        _ACTIVE.pop(dataset, None)


def active_jobs() -> dict:
    with _LOCK:
        return dict(_ACTIVE)


# ---------------------------------------------------------------- 作业体
def _run_etl(periods: list[str], operator: str, limit: int | None = None,
             with_ads: bool = True) -> dict:
    """DWD → DWS → ADS 全链路。"""
    results: dict[str, dict] = {}
    results["dwd"] = dwd_etl.run(periods, operator=operator, limit=limit)
    if _failures(results):
        return {"stages": results, "failed": _failures(results)}
    results["dws"] = dws_etl.run(periods, operator=operator)
    if _failures(results):
        return {"stages": results, "failed": _failures(results)}
    if with_ads:
        results["ads"] = ads_etl.run(periods, operator=operator)
    failed = [k for group in results.values() for k, v in group.items()
              if isinstance(v, dict) and v.get("status") == "failed"]
    return {"stages": results, "failed": failed}


def _job_ingest(dataset: str, periods: list[str], mode: str, operator: str,
                params: dict) -> dict:
    if dataset == "bts":
        return ods_etl.ingest_bts(periods, mode=mode, limit=params.get("limit"), operator=operator)
    if dataset == "isd":
        return ods_etl.ingest_isd(periods, mode=mode,
                                  max_stations=params.get("max_stations"), operator=operator)
    if dataset == "weatherevents":
        return ods_etl.ingest_weather_events(mode=mode, max_rows=params.get("limit"),
                                             operator=operator)
    if dataset == "ourairports":
        return dim_etl.build_dim_airport(operator=operator)
    if dataset == "openflights":
        return dim_etl.build_dim_airline(operator=operator)
    if dataset == "manifest":
        return _register_manifest(operator)
    raise ApiError(ErrCode.PARAM_INVALID,
                   f"数据集 {dataset} 无入湖通道（只读或需人工处理），"
                   f"可入湖的数据集：bts/isd/weatherevents/ourairports/openflights")


def _register_manifest(operator: str) -> dict:
    """把 00_清单与字典/manifest.csv 登记到 ods_file_manifest（通道 B）。"""
    from app.etl.common import upsert_manifest
    from app.extensions import db
    from app.models import FileManifest

    manifest = datasource.load_manifest_csv()
    count = 0
    for rel_path, entry in manifest.items():
        file_name = rel_path.split("/")[-1]
        upsert_manifest(entry.get("dataset", "manifest"), "", file_name, rel_path,
                        entry.get("md5", ""), 0, 0)
        count += 1
    total = db.session.query(FileManifest).count()
    return {"job_id": uuid.uuid4().hex[:12], "job_name": "register_manifest",
            "status": "success", "rows_in": count, "rows_out": total,
            "drop_rows": 0, "drop_reason": {}, "elapsed_sec": 0,
            "stages": [{"stage": "register", "note": f"登记 {count} 个文件"}]}


def _run_job(job_type: str, dataset: str, periods: list[str], mode: str,
             operator: str, params: dict) -> dict:
    if job_type == "dims":
        return {"dims": dim_etl.run_all(operator=operator)}
    if job_type == "ingest":
        return {"ingest": _job_ingest(dataset, periods, mode, operator, params)}
    if job_type == "etl":
        return _run_etl(periods, operator, limit=params.get("limit"))
    if job_type == "full":
        ingest = _job_ingest(dataset, periods, mode, operator, params)
        if _failures(ingest):
            return {"ingest": ingest}
        etl = _run_etl(periods, operator, limit=params.get("limit"))
        return {"ingest": ingest, **etl}
    if job_type == "train":
        task = params.get("task", "cls15")
        report = model_service.train_and_evaluate(
            periods, task=task, algo=params.get("algo", "auto"), operator=operator,
            params=params.get("hyperparameters"))
        predictions = []
        for period in (periods[-1:] if task == "cls15" else []):
            predictions.append(model_service.predict_and_store(
                period, model_ver=report["model_ver"], operator=operator))
        return {"train": report, "predict": predictions}
    raise ApiError(ErrCode.PARAM_INVALID, f"未知作业类型：{job_type}")


# ---------------------------------------------------------------- 对外接口
def create_job(job_type: str, dataset: str, periods: list[str] | None = None,
               mode: str = "incremental", operator: str = "system",
               params: dict | None = None, dry_run: bool = False,
               background: bool = True) -> dict:
    """创建作业：先做周期校验（文件级闸门），再入队。"""
    if job_type not in JOB_TYPES:
        raise ApiError(ErrCode.PARAM_INVALID, f"未知作业类型：{job_type}，可选：{list(JOB_TYPES)}")
    if mode not in ("full", "incremental"):
        raise ApiError(ErrCode.PARAM_INVALID, "mode 只能是 full 或 incremental")
    params = params or {}
    dataset = dataset or ("bts" if job_type in ("ingest", "full", "etl") else "system")

    if job_type in ("ingest", "full") and dataset in datasource.DATASETS:
        resolved = datasource.resolve_periods(dataset, periods, mode=mode)
    elif job_type in ("etl", "train"):
        resolved = periods or engine.BaseEngine.available_periods("dwd_flight_detail") or []
        if not resolved:
            resolved = datasource.available_periods("bts")[-1:]
        if not resolved:
            raise ApiError(ErrCode.DATA_NOT_READY, "没有任何可加工的月份，请先入湖")
    else:
        resolved = periods or [""]

    job_id = f"{job_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    preview = {
        "job_id": job_id, "job_type": job_type, "dataset": dataset,
        "periods": resolved, "mode": mode, "operator": operator,
        "params": params, "description": JOB_TYPES[job_type],
    }
    if dry_run:
        preview["dry_run"] = True
        preview["files"] = len(datasource.scan_files(dataset)) if dataset in datasource.DATASETS else None
        return preview

    _acquire(dataset, job_id)
    # 先在 MySQL 里落一条 queued，页面立刻能查到
    from app.extensions import db
    from app.models import JobRun

    row = JobRun(job_id=job_id, job_name=f"{job_type}:{dataset}", dataset=dataset,
                 period=",".join(resolved), mode=mode, engine=engine.get_engine().name,
                 status="queued", operator=operator,
                 started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    try:
        db.session.add(row)
        db.session.commit()
    except Exception:
        db.session.rollback()
        _release(dataset)
        raise

    def _target():
        # 后台线程使用自己的 Session 和 ORM 实例，不能跨线程传递创建者的 row。
        started = time.monotonic()
        try:
            row = db.session.query(JobRun).filter_by(job_id=job_id).one()
            row.status = "running"
            db.session.commit()
            result = _run_job(job_type, dataset, resolved, mode, operator, params)
            failures = _failures(result)
            row.status = "failed" if failures else "success"
            if failures:
                row.err_code = "E_STAGE_FAILED"
                row.err_msg = ", ".join(failures)[:2000]
            row.progress = 100.0
            row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            row.elapsed_sec = time.monotonic() - started
            row.stages = json.dumps(result, ensure_ascii=False)
            db.session.commit()
            # 数据变了，清看板缓存
            for ns in ("overview", "network", "airport", "route", "flight", "pred", "quality"):
                invalidate(ns)
            from app.services import stats
            stats.airport_dim.cache_clear()
            stats.airline_dim.cache_clear()
        except Exception as exc:
            log.exception("作业 %s 失败", job_id)
            try:
                db.session.rollback()
                row = db.session.query(JobRun).filter_by(job_id=job_id).one()
                row.status = "failed"
                row.err_code = str(getattr(exc, "code", "E_JOB_FAILED"))
                row.err_msg = str(exc)[:2000]
                row.elapsed_sec = time.monotonic() - started
                row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                db.session.commit()
            except Exception:
                db.session.rollback()
        finally:
            db.session.remove()
            _release(dataset)
            _THREADS.pop(job_id, None)

    if background:
        thread = threading.Thread(target=_target, name=f"job-{job_id}", daemon=True)
        _THREADS[job_id] = thread
        try:
            thread.start()
        except Exception:
            _THREADS.pop(job_id, None)
            _release(dataset)
            row.status = "failed"
            row.err_code = "E_THREAD_START"
            row.finished_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            db.session.commit()
            raise
        preview["queued"] = True
        return preview
    _target()
    preview["queued"] = False
    return preview


def job_status(job_id: str) -> dict:
    from app.extensions import db
    from app.models import JobRun

    row = db.session.query(JobRun).filter_by(job_id=job_id).first()
    if row is None:
        raise ApiError(ErrCode.NOT_FOUND, f"作业不存在：{job_id}")
    data = row.to_dict()
    data["running_thread"] = job_id in _THREADS
    return data


def job_list(limit: int = 20, dataset: str | None = None, status: str | None = None,
             period: str | None = None) -> list[dict]:
    from app.extensions import db
    from app.models import JobRun

    query = db.session.query(JobRun)
    if dataset:
        query = query.filter(JobRun.dataset == dataset)
    if status:
        query = query.filter(JobRun.status == status)
    if period:
        query = query.filter(JobRun.period.contains(period, autoescape=True))
    rows = query.order_by(JobRun.id.desc()).limit(limit).all()
    return [r.to_dict() for r in rows]


def job_stats(days: int = 30) -> dict:
    """P8 系统监控：作业耗时与失败重试趋势。"""
    from app.extensions import db
    from app.models import JobRun

    rows = (db.session.query(JobRun).order_by(JobRun.id.desc()).limit(500).all())
    by_name: dict[str, dict] = {}
    for row in rows:
        item = by_name.setdefault(row.job_name, {
            "job_name": row.job_name, "runs": 0, "success": 0, "failed": 0,
            "elapsed": [], "rows_out": 0,
        })
        item["runs"] += 1
        if row.status == "success":
            item["success"] += 1
        elif row.status in ("failed", "blocked"):
            item["failed"] += 1
        if row.elapsed_sec:
            item["elapsed"].append(row.elapsed_sec)
        item["rows_out"] += int(row.rows_out or 0)
    series = []
    for item in by_name.values():
        elapsed = item.pop("elapsed")
        item["avg_elapsed_sec"] = round(sum(elapsed) / len(elapsed), 1) if elapsed else 0
        item["max_elapsed_sec"] = round(max(elapsed), 1) if elapsed else 0
        item["success_rate"] = round(item["success"] / item["runs"], 4) if item["runs"] else None
        series.append(item)
    series.sort(key=lambda x: x["avg_elapsed_sec"], reverse=True)
    return {
        "window_days": days,
        "by_job": series,
        "total_runs": sum(i["runs"] for i in series),
        "failed_runs": sum(i["failed"] for i in series),
        "engine": engine.engine_info() if hasattr(engine, "engine_info") else {},
    }


# ---------------------------------------------------------------- 页面直传
def stage_upload(file_storage, dataset: str, username: str, checksum: str | None = None) -> dict:
    """通道 C：单文件 ≤45MB 直传，后缀白名单 + 防路径穿越（验收 V-8 / U-11 / U-12）。"""
    import hashlib

    from app.extensions import db
    from app.models import StagingFile

    if not file_storage or not file_storage.filename:
        raise ApiError(ErrCode.PARAM_MISSING, "缺少上传文件")
    raw_name = file_storage.filename
    # 只取 basename，拒绝 .. / 绝对路径 / 盘符 / UNC
    safe_name = Path(raw_name).name
    if (".." in raw_name) or ("/" in raw_name) or ("\\" in raw_name) or (":" in raw_name):
        raise ApiError(ErrCode.PARAM_INVALID, "文件名非法：不允许路径分隔符、盘符或 ../")
    ext = Path(safe_name).suffix.lower()
    if ext not in Config.ALLOWED_EXTENSIONS:
        raise ApiError(ErrCode.UNSUPPORTED,
                       f"不支持的文件类型 {ext}，白名单：{sorted(Config.ALLOWED_EXTENSIONS)}")

    staging_id = f"stg_{datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"
    target_dir = Path(Config.STAGING_ROOT) / staging_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / safe_name
    file_storage.save(str(target))

    size = target.stat().st_size
    if size > Config.STAGING_MAX_BYTES:
        target.unlink(missing_ok=True)
        raise ApiError(ErrCode.TOO_LARGE,
                       f"文件 {size / 1024 / 1024:.1f} MB 超过 staging 上限 "
                       f"{Config.STAGING_MAX_BYTES / 1024 / 1024:.0f} MB（大文件请走通道 A）")

    digest = hashlib.md5()
    with open(target, "rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(block)
    md5 = digest.hexdigest()
    if checksum and checksum.lower() != md5:
        target.unlink(missing_ok=True)
        raise ApiError(ErrCode.PARAM_INVALID, f"校验失败：声明 {checksum}，实测 {md5}")

    row = StagingFile(staging_id=staging_id, dataset=dataset or "custom", file_name=safe_name,
                      file_path=str(target), file_md5=md5, file_bytes=size,
                      uploaded_by=username, uploaded_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    db.session.add(row)
    db.session.commit()
    return row.to_dict()


def promote_staging(staging_id: str, operator: str) -> dict:
    import shutil

    from app.extensions import db
    from app.models import StagingFile

    row = db.session.query(StagingFile).filter_by(staging_id=staging_id).first()
    if row is None:
        raise ApiError(ErrCode.NOT_FOUND, f"staging 记录不存在：{staging_id}")
    if row.promoted:
        return {"staging_id": staging_id, "promoted": True, "message": "已转正，无需重复操作"}
    source = Path(row.file_path)
    if not source.exists():
        raise ApiError(ErrCode.NOT_FOUND, "staging 文件已丢失，请重新上传")
    target_dir = Path(Config.DATA_ROOT) / "08_人工补录"
    target_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target_dir / row.file_name)
    row.promoted = 1
    row.promoted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    db.session.commit()
    log.info("staging %s 转正到 %s（操作人 %s）", staging_id, target_dir, operator)
    return {"staging_id": staging_id, "promoted": True, "target": str(target_dir / row.file_name)}


def dead_letters(limit: int = 100, include_ignored: bool = False) -> list[dict]:
    from app.extensions import db
    from app.models import IngestFailed

    query = db.session.query(IngestFailed)
    if not include_ignored:
        query = query.filter(IngestFailed.ignored == 0)
    return [r.to_dict() for r in query.order_by(IngestFailed.id.desc()).limit(limit).all()]


def resolve_dead_letter(letter_id: int, action: str, operator: str) -> dict:
    from app.extensions import db
    from app.models import IngestFailed

    row = db.session.query(IngestFailed).filter_by(id=letter_id).first()
    if row is None:
        raise ApiError(ErrCode.NOT_FOUND, f"死信不存在：{letter_id}")
    if action == "ignore":
        row.ignored = 1
        db.session.commit()
        return {"id": letter_id, "ignored": True}
    if action == "retry":
        job = create_job("full" if row.dataset in ("bts",) else "ingest", row.dataset,
                         [row.period] if row.period else None, mode="full", operator=operator)
        return {"id": letter_id, "retry_job": job}
    raise ApiError(ErrCode.PARAM_INVALID, "action 只能是 retry 或 ignore")
