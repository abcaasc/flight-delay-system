"""统一响应结构与错误码（对应设计说明书 §6.2 接口契约）。

约定：
    {"code": 0, "message": "success", "data": {...}, "trace_id": "..."}
code 分四类：
    40x  参数错误        400 缺参 / 422 取值非法 / 404 资源不存在
    41x  权限错误        401 未认证 / 403 角色不足
    50x  服务错误        500 未捕获 / 503 依赖不可用
    52x  数据缺失        521 数据未就绪（请先跑 ETL）/ 522 查询结果为空
"""
from __future__ import annotations

import uuid
from typing import Any

from flask import g, jsonify


class ErrCode:
    OK = 0
    PARAM_MISSING = 400
    UNAUTHORIZED = 401
    FORBIDDEN = 403
    NOT_FOUND = 404
    PARAM_INVALID = 422
    TOO_LARGE = 413
    UNSUPPORTED = 415
    SERVER_ERROR = 500
    UNAVAILABLE = 503
    DATA_NOT_READY = 521
    EMPTY_RESULT = 522


ERR_MESSAGE = {
    ErrCode.PARAM_MISSING: "缺少必要参数",
    ErrCode.UNAUTHORIZED: "未认证或登录已过期",
    ErrCode.FORBIDDEN: "当前角色无权访问该资源",
    ErrCode.NOT_FOUND: "资源不存在",
    ErrCode.PARAM_INVALID: "参数取值非法",
    ErrCode.TOO_LARGE: "请求体超出大小限制",
    ErrCode.UNSUPPORTED: "不支持的文件类型",
    ErrCode.SERVER_ERROR: "服务内部错误",
    ErrCode.UNAVAILABLE: "依赖服务不可用",
    ErrCode.DATA_NOT_READY: "数据尚未就绪，请先执行数据接入与 ETL",
    ErrCode.EMPTY_RESULT: "查询结果为空",
}


class ApiError(Exception):
    """业务异常，由全局 handler 统一转成响应体。"""

    def __init__(self, code: int = ErrCode.SERVER_ERROR, message: str | None = None,
                 data: Any = None, http_status: int | None = None):
        super().__init__(message or ERR_MESSAGE.get(code, "error"))
        self.code = code
        self.message = message or ERR_MESSAGE.get(code, "error")
        self.data = data
        self.http_status = http_status or _default_http_status(code)


def _default_http_status(code: int) -> int:
    if code == 0:
        return 200
    if code in (ErrCode.PARAM_MISSING, ErrCode.PARAM_INVALID, ErrCode.TOO_LARGE, ErrCode.UNSUPPORTED):
        return 400
    if code == ErrCode.UNAUTHORIZED:
        return 401
    if code == ErrCode.FORBIDDEN:
        return 403
    if code == ErrCode.NOT_FOUND:
        return 404
    if code in (ErrCode.UNAVAILABLE, ErrCode.DATA_NOT_READY, ErrCode.EMPTY_RESULT):
        # 数据缺失不改变 HTTP 语义，页面按 code 渲染四态
        return 200
    return 500


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def current_trace_id() -> str:
    tid = getattr(g, "trace_id", None)
    if not tid:
        tid = new_trace_id()
        g.trace_id = tid
    return tid


def ok(data: Any = None, message: str = "success", **extra: Any):
    body = {"code": ErrCode.OK, "message": message, "data": data, "trace_id": current_trace_id()}
    if extra:
        body.update(extra)
    return jsonify(body)


def fail(code: int, message: str | None = None, data: Any = None, http_status: int | None = None):
    body = {
        "code": code,
        "message": message or ERR_MESSAGE.get(code, "error"),
        "data": data,
        "trace_id": current_trace_id(),
    }
    resp = jsonify(body)
    resp.status_code = http_status or _default_http_status(code)
    return resp


def page_payload(items: list, total: int, page: int, page_size: int, **extra: Any) -> dict:
    payload = {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": (total + page_size - 1) // page_size if page_size else 0,
    }
    payload.update(extra)
    return payload
