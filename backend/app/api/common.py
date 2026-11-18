"""接口层公共装饰器与响应辅助。"""
from __future__ import annotations

import csv
import functools
import io
from datetime import datetime
from itertools import islice
from typing import Callable, Iterable

from flask import Response, request

from app.config import Config


def cache_headers(max_age: int = 300) -> Callable:
    """显式声明缓存语义（§6.2 接口契约：GET 默认 5 分钟，看板 10 分钟）。"""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            response = fn(*args, **kwargs)
            try:
                response.headers["Cache-Control"] = f"public, max-age={max_age}"
                response.headers["X-Cache-TTL"] = str(max_age)
            except AttributeError:
                pass
            return response

        return wrapper

    return decorator


def no_cache(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        response = fn(*args, **kwargs)
        try:
            response.headers["Cache-Control"] = "no-store"
        except AttributeError:
            pass
        return response

    return wrapper


def csv_response(rows: Iterable[dict], filename_prefix: str,
                 max_rows: int = Config.EXPORT_MAX_ROWS) -> Response:
    """导出 CSV（带 BOM，Excel 直接打开不乱码）。默认最多 1000 行，防拖库。"""
    rows = list(islice(rows, max_rows))
    buffer = io.StringIO()
    if rows:
        writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(v) for k, v in row.items()})
    else:
        buffer.write("提示\n无符合条件的数据\n")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = "\ufeff" + buffer.getvalue()
    return Response(
        payload,
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename_prefix}_{stamp}.csv"',
            "Cache-Control": "no-store",
            "X-Max-Rows": str(max_rows),
        },
    )


def client_ip() -> str:
    return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()


def _csv_cell(value):
    if value is None:
        return ""
    # 导入数据里的文本不能在 Excel 中被解释为公式；数值负延误仍保持数值。
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value
