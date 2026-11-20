"""Flask 应用工厂。"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException, NotFound

from app.config import BASE_DIR, Config, ensure_dirs
from app.extensions import cors, db
from app.utils.logger import setup_logging
from app.utils.response import ApiError, ErrCode, current_trace_id, fail, new_trace_id, ok


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    ensure_dirs()
    setup_logging(Config.LOG_LEVEL, log_dir=BASE_DIR / "instance")
    log = logging.getLogger("app")

    # ---------------- 扩展 ----------------
    cors.init_app(app, resources={r"/api/*": {"origins": "*"}},
                  allow_headers=["Content-Type", "Authorization"], expose_headers=["X-Trace-Id"])

    # ---------------- 数据库 ----------------
    with app.app_context():
        db.create_all()
        from app.services.job_runtime import reconcile_jobs

        reconcile_jobs()

    @app.teardown_appcontext
    def _close_session(exc=None):
        # scoped_session 按线程复用，必须在请求/CLI 结束后释放事务及连接。
        db.session.remove()

    # ---------------- 中间件 ----------------
    @app.before_request
    def _before():
        g.trace_id = request.headers.get("X-Trace-Id") or new_trace_id()
        g.start_time = time.time()

    @app.after_request
    def _after(response):
        response.headers["X-Trace-Id"] = current_trace_id()
        try:
            elapsed_ms = int((time.time() - g.get("start_time", time.time())) * 1000)
            response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        except Exception:
            pass
        return response

    # ---------------- 错误处理 ----------------
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return fail(exc.code, exc.message, exc.data, exc.http_status)

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        code = {400: ErrCode.PARAM_INVALID, 401: ErrCode.UNAUTHORIZED, 403: ErrCode.FORBIDDEN,
                404: ErrCode.NOT_FOUND, 413: ErrCode.TOO_LARGE, 415: ErrCode.UNSUPPORTED}.get(
            exc.code, ErrCode.SERVER_ERROR)
        return fail(code, exc.description or exc.name, http_status=exc.code if exc.code < 500 else 500)

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception("未捕获异常：%s %s", request.method, request.path)
        return fail(ErrCode.SERVER_ERROR, f"服务内部错误：{type(exc).__name__}", http_status=500)

    # ---------------- 路由 ----------------
    from app.api import register_blueprints

    register_blueprints(app)

    from app.cli import register_commands

    register_commands(app)

    # ---------------- 前端静态资源 ----------------
    # 单容器部署时后端同时托管 Vue 构建产物：`/` 出首页，深链接回落到 index.html，
    # /api/* 与 /static/* 不参与回落（否则接口 404 会被吞成一张网页）。
    dist = Path(config_object.FRONTEND_DIST)
    has_frontend = (dist / "index.html").is_file()

    def index():
        return ok({
            "name": "基于 Spark 的航班延误预测与航线网络态势可视化系统",
            "version": "1.0.0",
            "api_prefix": "/api/v1",
            "modules": {
                "auth": "/api/v1/auth/login",
                "dashboard": "/api/v1/overview",
                "network": "/api/v1/network",
                "predict": "/api/v1/predict",
                "model": "/api/v1/model/versions",
                "data": "/api/v1/ingest/datasets",
                "system": "/api/v1/system/health",
                "ai": "/api/v1/ai/chat",
            },
            "docs": "/api/v1/system/endpoints",
            "health": "/api/v1/system/health",
        })

    if has_frontend:
        @app.get("/", endpoint="spa_index")
        def spa_index():
            return send_from_directory(dist, "index.html", max_age=0)

        @app.get("/<path:asset>", endpoint="spa_asset")
        def spa_asset(asset: str):
            if asset.startswith(("api/", "static/")):
                raise NotFound()
            target = dist / asset
            if asset and target.is_file():
                return send_from_directory(dist, asset, max_age=86400)
            return send_from_directory(dist, "index.html", max_age=0)

        # 有前端时把模块清单后移到 /api/v1，避免占用首页
        app.add_url_rule("/api/v1", "index", index)
        log.info("已托管前端静态资源：%s", dist)
    else:
        app.add_url_rule("/", "index", index)
        app.add_url_rule("/api/v1", "api_index", index)

    @app.get("/api/v1/system/endpoints")
    def endpoints():
        """接口清单（自动从 url_map 生成，避免文档与代码不一致）。"""
        items = []
        for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
            if not str(rule).startswith("/api"):
                continue
            methods = sorted(m for m in rule.methods if m not in ("HEAD", "OPTIONS"))
            items.append({
                "path": str(rule),
                "methods": methods,
                "endpoint": rule.endpoint,
                "doc": (app.view_functions[rule.endpoint].__doc__ or "").strip().splitlines()[:1],
            })
        return ok({"count": len(items), "items": items})

    log.info("应用已创建：env=%s，数据源=%s", Config.ENV, Config.DATA_ROOT)
    return app


__all__ = ["create_app"]
