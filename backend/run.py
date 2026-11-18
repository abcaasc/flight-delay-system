"""服务入口。

开发：python run.py            （Flask 内置服务器 + 调试）
生产：waitress-serve --port=5000 run:app
"""
from __future__ import annotations

import os
from sqlalchemy.engine import make_url

from app import create_app
from app.config import Config

app = create_app()


def _serve_production(host: str, port: int) -> None:
    """生产环境用 waitress（纯 Python WSGI 服务器，Windows/Linux 通用）。"""
    try:
        from waitress import serve
    except ImportError:  # pragma: no cover - 兜底：没装 waitress 时仍能起服务
        app.run(host=host, port=port, threaded=True)
        return
    serve(app, host=host, port=port, threads=8, channel_timeout=120)


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "5000"))
    debug = Config.ENV == "dev"
    print(f"""
============================================================
 航班延误预测与航线网络态势可视化系统 · 后端服务
 环境      : {Config.ENV}
 数据源    : {Config.DATA_ROOT}
 数仓目录  : {Config.WAREHOUSE_ROOT}
 数据库    : {make_url(Config.DATABASE_URL).render_as_string(hide_password=True)}
 监听      : http://{host}:{port}
 接口清单  : http://127.0.0.1:{port}/api/v1/system/endpoints
============================================================
""")
    if debug:
        app.run(host=host, port=port, debug=True, threaded=True)
    else:
        _serve_production(host, port)
