"""Flask CLI 命令：init-db / seed / ingest / etl / train / pipeline / doctor。

用法示例：
    flask --app run.py init-db
    flask --app run.py seed-users
    flask --app run.py ingest --dataset bts --periods 2015-01 --mode full
    flask --app run.py etl --periods 2015-01
    flask --app run.py pipeline --periods 2015-01,2015-02
    flask --app run.py train --periods 2015-01,2015-02 --task cls15
    flask --app run.py doctor
"""
from __future__ import annotations

import json
import time

import click
from flask import Flask

from app.config import Config
from app.extensions import db
from app.models import AlertRule, User
from app.utils.auth import ROLE_NAMES, hash_password
from app.utils.logger import get_logger

log = get_logger(__name__)

DEFAULT_USERS = (
    ("guest", "guest123", "guest", "访客/评审"),
    ("ops", "ops123", "ops", "运行监控员"),
    ("analyst", "analyst123", "analyst", "数据分析员"),
    ("admin", "admin123", "admin", "系统管理员"),
)


def _split(text: str | None) -> list[str]:
    if not text:
        return []
    return [x.strip() for x in text.replace(" ", ",").split(",") if x.strip()]


def register_commands(app: Flask) -> None:
    @app.cli.command("init-db")
    def init_db():
        """建表（幂等）。"""
        db.create_all()
        from sqlalchemy.engine import make_url
        click.echo(f"数据库就绪：{make_url(Config.DATABASE_URL).render_as_string(hide_password=True)}")
        click.echo(f"数仓目录：{Config.WAREHOUSE_ROOT}")

    @app.cli.command("seed-users")
    def seed_users():
        """预置 4 个账号（guest/ops/analyst/admin）+ 默认告警规则。"""
        created = []
        for username, password, role, display in DEFAULT_USERS:
            if db.session.query(User).filter_by(username=username).first():
                continue
            db.session.add(User(username=username, password_hash=hash_password(password),
                                role=role, display_name=display))
            created.append(f"{username}/{password}（{ROLE_NAMES[role]}）")
        if not db.session.query(AlertRule).filter_by(name="延误率超过30%").first():
            db.session.add(AlertRule(name="延误率超过30%", metric="delay_rate", comparator="gt",
                                     threshold=0.30, scope_type="airport", scope_value="*",
                                     level="warning", created_by="system"))
        if not db.session.query(AlertRule).filter_by(name="取消率超过10%").first():
            db.session.add(AlertRule(name="取消率超过10%", metric="cancel_rate", comparator="gt",
                                     threshold=0.10, scope_type="airport", scope_value="*",
                                     level="critical", created_by="system"))
        db.session.commit()
        click.echo("新建账号：" + ("; ".join(created) if created else "已存在，跳过"))
        click.echo("告警规则已就绪（延误率>30%、取消率>10%）")

    @app.cli.command("data-scan")
    def data_scan():
        """扫描 9 个数据集，打印文件数与体量。"""
        from app.services import datasource

        for item in datasource.dataset_status():
            click.echo(f"{item['key']:14s} {item['name'][:22]:24s} "
                       f"files={item['files']:5d} {item['mb']:9.1f} MB "
                       f"periods={item['periods_found']:3d} "
                       f"missing={item['missing_count']:3d} ready={item['ready']}")

    @app.cli.command("verify")
    @click.option("--dataset", required=True)
    @click.option("--periods", default=None)
    @click.option("--no-md5", is_flag=True, default=False)
    def verify(dataset: str, periods: str | None, no_md5: bool):
        """文件级校验（MD5 + 字节数 + 周期完整）。"""
        from app.services import datasource

        report = datasource.verify_dataset(dataset, _split(periods) or None, calc_md5=not no_md5)
        click.echo(json.dumps({k: v for k, v in report.items() if k != "mismatch"},
                              ensure_ascii=False, indent=2))

    @app.cli.command("ingest")
    @click.option("--dataset", required=True)
    @click.option("--periods", default=None, help="逗号分隔，如 2015-01,2015-02")
    @click.option("--mode", default="incremental", type=click.Choice(["full", "incremental"]))
    @click.option("--limit", default=None, type=int, help="仅处理前 N 行（调试用）")
    def ingest(dataset: str, periods: str | None, mode: str, limit: int | None):
        """入湖（ODS/DIM）。"""
        from app.services import ingest as ingest_service

        job = ingest_service.create_job("ingest", dataset, _split(periods) or None, mode=mode,
                                        operator="cli", params={"limit": limit}, background=False)
        click.echo(json.dumps(job, ensure_ascii=False, indent=2, default=str))

    @app.cli.command("dims")
    def dims():
        """重建维表（机场/航司/日历）。"""
        from app.etl import dim as dim_etl

        click.echo(json.dumps(dim_etl.run_all(operator="cli"), ensure_ascii=False, indent=2,
                              default=str))

    @app.cli.command("etl")
    @click.option("--periods", default=None, help="逗号分隔；缺省=最新可用月份")
    @click.option("--table", default=None,
                  help="只跑某张表（dwd_flight_detail/dws_airport_hourly/...）")
    @click.option("--limit", default=None, type=int)
    def etl(periods: str | None, table: str | None, limit: int | None):
        """数仓加工 DWD → DWS → ADS。"""
        from app.etl import ads as ads_etl, dwd as dwd_etl, dws as dws_etl
        from app.services import engine

        targets = _split(periods) or engine.BaseEngine.available_periods("ods_bts_flight")[-1:]
        if not targets:
            raise click.ClickException("没有可加工的月份，请先 ingest")
        click.echo(f"加工月份：{targets}")
        if table in (None, "dwd_flight_detail", "dwd_weather_hourly"):
            click.echo(json.dumps(dwd_etl.run(targets, operator="cli", limit=limit),
                                  ensure_ascii=False, indent=2, default=str))
        if table in (None, "dws_airport_hourly", "dws_route_daily", "dws_delay_network",
                     "dws_flight_features"):
            click.echo(json.dumps(dws_etl.run(targets, operator="cli"),
                                  ensure_ascii=False, indent=2, default=str))
        if table in (None, "ads_airport_rank", "ads_anomaly_alert"):
            click.echo(json.dumps(ads_etl.run(targets, operator="cli"),
                                  ensure_ascii=False, indent=2, default=str))

    @app.cli.command("train")
    @click.option("--periods", default=None, help="逗号分隔；缺省=最近 3 个可用月份")
    @click.option("--task", default="cls15", type=click.Choice(["cls15", "level3", "reg"]))
    @click.option("--predict/--no-predict", default=True, help="训练后是否批量预测")
    @click.option("--online", is_flag=True, default=False, help="训练完直接上线（等价 admin 审批）")
    @click.option("--profile", default="warehouse", type=click.Choice(["warehouse", "form"]))
    @click.option("--sample-per-month", default=0, type=click.IntRange(min=0),
                  help="表单模型每月随机抽样条数，0 表示全量")
    def train(periods: str | None, task: str, predict: bool, online: bool,
              profile: str, sample_per_month: int):
        """训练 + 评测 + 批量预测（可选上线）。"""
        from app.extensions import db
        from app.models import ModelVersion
        from app.services import engine, model as model_service

        targets = _split(periods) or engine.BaseEngine.available_periods("dws_flight_features")[-3:]
        if not targets:
            raise click.ClickException("特征宽表为空，请先跑 etl")
        if sample_per_month and profile != "form":
            raise click.ClickException("--sample-per-month 仅用于表单模型")
        report = model_service.train_and_evaluate(
            targets, task=task, operator="cli",
            params={"feature_profile": profile, "sample_per_month": sample_per_month})
        if profile == "form":
            import os
            from tempfile import NamedTemporaryFile

            path = Config.FORM_MODEL_REPORT
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
                json.dump(report, file, ensure_ascii=False, indent=2)
            os.replace(file.name, path)
        click.echo(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        if online:
            db.session.query(ModelVersion).update({ModelVersion.is_online: 0})
            row = db.session.query(ModelVersion).filter_by(model_ver=report["model_ver"]).first()
            if row is not None:
                row.is_online = 1
                db.session.commit()
                click.secho(f"已上线：{report['model_ver']}", fg="green")
        if predict:
            for period in targets[-1:]:
                version = report["model_ver"] if task == "cls15" else None
                click.echo(json.dumps(model_service.predict_and_store(period, model_ver=version, operator="cli"),
                                      ensure_ascii=False, indent=2, default=str))

    @app.cli.command("pipeline")
    @click.option("--periods", default=None, help="逗号分隔；缺省=数据源里最新的 1 个月")
    @click.option("--limit", default=None, type=int, help="每步只取前 N 行（快速验证）")
    @click.option("--skip-ingest", is_flag=True, default=False)
    def pipeline(periods: str | None, limit: int | None, skip_ingest: bool):
        """一键全链路：入湖 → DWD → DWS → ADS → 训练 → 预测。"""
        from app.etl import ads as ads_etl, dim as dim_etl, dwd as dwd_etl, dws as dws_etl, ods as ods_etl
        from app.services import datasource, engine, model as model_service

        targets = _split(periods) or datasource.available_periods("bts")[-1:]
        if not targets:
            raise click.ClickException("数据源里没有 BTS 月份文件")
        started = time.time()
        click.secho(f"== 目标月份：{targets} ==", fg="cyan")

        if not skip_ingest:
            click.secho("[1/6] 维表", fg="green")
            click.echo(json.dumps(dim_etl.run_all(operator="cli"), ensure_ascii=False, default=str))
            click.secho("[2/6] BTS 入湖", fg="green")
            click.echo(json.dumps(ods_etl.ingest_bts(targets, mode="full", limit=limit,
                                                     operator="cli"), ensure_ascii=False, default=str))
        click.secho("[3/6] DWD（清洗 + 航班链 + 气象对齐）", fg="green")
        click.echo(json.dumps(dwd_etl.run(targets, operator="cli", limit=limit),
                              ensure_ascii=False, default=str))
        click.secho("[4/6] DWS 汇总", fg="green")
        click.echo(json.dumps(dws_etl.run(targets, operator="cli"), ensure_ascii=False, default=str))
        click.secho("[5/6] ADS 排行与预警", fg="green")
        click.echo(json.dumps(ads_etl.run(targets, operator="cli"), ensure_ascii=False, default=str))
        click.secho("[6/6] 训练与批量预测", fg="green")
        try:
            report = model_service.train_and_evaluate(targets, task="cls15", operator="cli")
            click.echo(json.dumps(report["splits"], ensure_ascii=False, default=str))
            click.echo(json.dumps(model_service.predict_and_store(targets[-1], operator="cli"),
                                  ensure_ascii=False, default=str))
        except Exception as exc:
            click.secho(f"训练步骤失败：{exc}", fg="yellow")
        click.secho(f"完成，用时 {time.time() - started:.1f}s", fg="cyan")
        click.echo(json.dumps(engine.table_overview(), ensure_ascii=False, indent=2, default=str))

    @app.cli.command("doctor")
    def doctor():
        """环境自检：依赖、数据源、数仓、引擎。"""
        import importlib

        click.secho("== 依赖 ==", fg="cyan")
        for module in ("flask", "pandas", "numpy", "pyarrow", "sqlalchemy", "pymysql", "redis",
                       "jwt", "pyspark", "lightgbm"):
            try:
                importlib.import_module(module)
                click.echo(f"  ✅ {module}")
            except Exception as exc:
                click.echo(f"  ⚠️  {module}: {exc}")

        from app.services import datasource, engine
        from app.services.spark_session import engine_info

        click.secho("== 数据源 ==", fg="cyan")
        click.echo(f"  DATA_ROOT = {Config.DATA_ROOT} (exists={Config.DATA_ROOT.exists()})")
        for item in datasource.dataset_status():
            click.echo(f"  {item['key']:14s} files={item['files']:5d} {item['mb']:9.1f} MB")

        click.secho("== 引擎 ==", fg="cyan")
        click.echo(f"  查询引擎 = {engine.get_engine().name}")
        click.echo(f"  Spark      = {engine_info()}")

        click.secho("== 数仓 ==", fg="cyan")
        for row in engine.table_overview():
            flag = "✅" if row["ready"] else "—"
            click.echo(f"  {flag} {row['table']:22s} rows={row['row_count']:>10d} "
                       f"partitions={row['partitions']:>3d}")

