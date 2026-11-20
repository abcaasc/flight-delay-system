"""数据管理接口（M1）：数据集、校验、入湖作业、质量报告、表浏览、死信、直传。"""
from __future__ import annotations

from flask import Blueprint, request

from app.config import Config
from app.services import datasource, engine, ingest, stats
from app.utils import params
from app.utils.auth import current_identity, require_role, write_audit
from app.utils.response import ApiError, ErrCode, ok

bp = Blueprint("data", __name__, url_prefix="/api/v1")


# ---------------------------------------------------------------- 数据集与校验
@bp.get("/ingest/datasets")
@require_role("ops")
def datasets():
    return ok({
        "data_root": str(Config.DATA_ROOT),
        "warehouse_root": str(Config.WAREHOUSE_ROOT),
        "items": datasource.dataset_status(),
        "channels": {
            "A": "命令行/调度批处理（数据面，大文件本地读取）",
            "B": "控制面（只传指令与清单）",
            "C": "页面直传（单文件 ≤50MB）",
        },
        "active_jobs": ingest.active_jobs(),
    })


@bp.post("/ingest/verify")
@require_role("admin")
def verify():
    body = params.json_body(required=False)
    dataset = params.body_str(body, "dataset", required=True, max_len=32)
    periods = body.get("periods") or []
    calc_md5 = body.get("calc_md5", True)
    if periods and not isinstance(periods, list):
        raise ApiError(ErrCode.PARAM_INVALID, "periods 必须是数组")
    report = datasource.verify_dataset(dataset, periods or None, calc_md5=bool(calc_md5))
    write_audit("ingest_verify", dataset, f"passed={report['passed']} "
                                          f"files={report['files_found']}")
    return ok(report)


@bp.get("/ingest/datasets/<dataset>/files")
@require_role("ops")
def dataset_files(dataset: str):
    files = datasource.scan_files(dataset)
    page, size = params.page_args(default_size=50)
    start = (page - 1) * size
    return ok({
        "dataset": dataset,
        "total": len(files),
        "page": page,
        "page_size": size,
        "items": files[start:start + size],
        "periods": datasource.available_periods(dataset),
    })


# ---------------------------------------------------------------- 作业
@bp.post("/ingest/jobs")
@require_role("admin")
def create_job():
    body = params.json_body()
    job_type = params.body_str(body, "job_type", default="full", max_len=16)
    dataset = params.body_str(body, "dataset", default="bts", max_len=32)
    periods = body.get("periods") or []
    if periods and not isinstance(periods, list):
        raise ApiError(ErrCode.PARAM_INVALID, "periods 必须是数组")
    mode = params.body_str(body, "mode", default="incremental", max_len=16)
    dry_run = bool(body.get("dry_run", False))
    extra = body.get("params") or {}
    if not isinstance(extra, dict):
        raise ApiError(ErrCode.PARAM_INVALID, "params 必须是对象")

    job = ingest.create_job(job_type, dataset, periods, mode=mode,
                            operator=current_identity()["username"], params=extra,
                            dry_run=dry_run)
    if not dry_run:
        write_audit("ingest_job", dataset,
                    f"job_type={job_type} periods={job['periods']} job_id={job['job_id']}")
    return ok(job)


@bp.get("/ingest/jobs")
@require_role("ops")
def list_jobs():
    return ok({
        "items": ingest.job_list(
            limit=params.get_int("limit", 20, minimum=1, maximum=200),
            dataset=params.get_str("dataset"),
            status=params.get_str("status"),
            period=params.get_str("period"),
        ),
        "active": ingest.active_jobs(),
    })


@bp.get("/ingest/jobs/<job_id>")
@require_role("ops")
def job_status(job_id: str):
    return ok(ingest.job_status(job_id))


@bp.post("/ingest/jobs/<job_id>/retry")
@require_role("admin")
def retry_job(job_id: str):
    result = ingest.retry_job(job_id, current_identity()["username"])
    write_audit("job_retry", job_id, f"新作业 {result['job_id']}")
    return ok(result)


@bp.get("/ingest/failed")
@require_role("admin")
def dead_letters():
    return ok({"items": ingest.dead_letters(
        limit=params.get_int("limit", 100, minimum=1, maximum=500),
        include_ignored=params.get_bool("include_ignored"),
    )})


@bp.post("/ingest/failed/<int:letter_id>")
@require_role("admin")
def resolve_failed(letter_id: int):
    body = params.json_body()
    action = params.body_str(body, "action", required=True, max_len=16)
    result = ingest.resolve_dead_letter(letter_id, action, current_identity()["username"])
    write_audit("dead_letter", str(letter_id), f"action={action}")
    return ok(result)


# ---------------------------------------------------------------- 质量与表浏览
@bp.get("/quality/report")
@require_role("ops")
def quality():
    return ok(stats.quality_report(params.get_str("period")))


@bp.get("/warehouse/tables")
@require_role("ops")
def warehouse_tables():
    return ok({
        "engine": engine.get_engine().name,
        "layers": ["ods", "dim", "dwd", "dws", "ads"],
        "items": engine.table_overview(),
    })


@bp.get("/warehouse/tables/<table>/partitions")
@require_role("ops")
def table_partitions(table: str):
    if table not in engine.TABLES:
        raise ApiError(ErrCode.NOT_FOUND, f"未知数仓表：{table}")
    return ok({
        "table": table,
        "schema": engine.get_table(table).columns,
        "partitions": engine.BaseEngine.partitions(table),
    })


# ---------------------------------------------------------------- 页面直传
@bp.post("/upload/staging")
@require_role("admin")
def upload_staging():
    file = request.files.get("file")
    dataset = request.form.get("dataset", "custom")
    checksum = request.form.get("checksum")
    row = ingest.stage_upload(file, dataset, current_identity()["username"], checksum)
    write_audit("upload_staging", row["staging_id"], f"{row['file_name']} {row['file_bytes']}B")
    return ok(row)


@bp.post("/upload/promote")
@require_role("admin")
def promote():
    body = params.json_body()
    staging_id = params.body_str(body, "staging_id", required=True, max_len=48)
    result = ingest.promote_staging(staging_id, current_identity()["username"])
    write_audit("upload_promote", staging_id, str(result.get("target", "")))
    return ok(result)


@bp.get("/upload/staging")
@require_role("admin")
def list_staging():
    from app.extensions import db
    from app.models import StagingFile

    rows = db.session.query(StagingFile).order_by(StagingFile.id.desc()).limit(100).all()
    return ok({"items": [r.to_dict() for r in rows]})
