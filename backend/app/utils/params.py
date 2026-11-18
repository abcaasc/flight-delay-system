"""请求参数解析与校验（统一 40x 错误）。"""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Iterable

from flask import request

from app.config import Config
from app.utils.response import ApiError, ErrCode
from app.utils.timeutil import TimeParseError, parse_date, parse_ym


def _raw(name: str) -> str | None:
    value = request.args.get(name)
    if value is None and request.method in {"POST", "PUT", "PATCH"}:
        body = json_body(required=False)
        value = body.get(name)
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    return str(value).strip()


def get_str(name: str, default: str | None = None, *, required: bool = False,
            choices: Iterable[str] | None = None, max_len: int = 512) -> str | None:
    value = _raw(name)
    if value in (None, ""):
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要参数：{name}")
        return default
    if len(value) > max_len:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 超长（>{max_len}）")
    if choices is not None and value not in set(choices):
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 取值非法，可选：{sorted(set(choices))}")
    return value


def get_int(name: str, default: int | None = None, *, required: bool = False,
            minimum: int | None = None, maximum: int | None = None) -> int | None:
    value = _raw(name)
    if value in (None, ""):
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要参数：{name}")
        return default
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number != number.to_integral_value():
            raise ValueError
        out = int(number)
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 必须是整数")
    if minimum is not None and out < minimum:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 不能小于 {minimum}")
    if maximum is not None and out > maximum:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 不能大于 {maximum}")
    return out


def get_float(name: str, default: float | None = None, *, minimum: float | None = None,
              maximum: float | None = None) -> float | None:
    value = _raw(name)
    if value in (None, ""):
        return default
    try:
        out = float(value)
        if not math.isfinite(out):
            raise ValueError
    except (TypeError, ValueError):
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 必须是数值")
    if minimum is not None and out < minimum:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 不能小于 {minimum}")
    if maximum is not None and out > maximum:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 不能大于 {maximum}")
    return out


def get_bool(name: str, default: bool = False) -> bool:
    value = _raw(name)
    if value in (None, ""):
        return default
    if value.lower() not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ApiError(ErrCode.PARAM_INVALID, f"参数 {name} 必须是布尔值")
    return value.lower() in {"1", "true", "yes", "on"}


def get_list(name: str, *, upper: bool = False) -> list[str]:
    value = _raw(name)
    if not value:
        return []
    items = [x.strip() for x in value.replace(" ", ",").split(",") if x.strip()]
    return [x.upper() for x in items] if upper else items


def get_date(name: str, default: date | None = None, *, required: bool = False) -> date | None:
    value = _raw(name)
    if not value:
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要参数：{name}")
        return default
    try:
        return parse_date(value)
    except TimeParseError as exc:
        raise ApiError(ErrCode.PARAM_INVALID, str(exc))


def get_ym(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = _raw(name)
    if not value:
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要参数：{name}")
        return default
    try:
        return parse_ym(value)
    except TimeParseError as exc:
        raise ApiError(ErrCode.PARAM_INVALID, str(exc))


def page_args(default_size: int | None = None) -> tuple[int, int]:
    page = get_int("page", 1, minimum=1) or 1
    size = get_int("page_size", default_size or Config.DEFAULT_PAGE_SIZE, minimum=1,
                   maximum=Config.MAX_PAGE_SIZE) or Config.DEFAULT_PAGE_SIZE
    return page, size


def json_body(required: bool = True) -> dict:
    body = request.get_json(silent=True)
    if body is None:
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, "请求体必须是 JSON")
        return {}
    if not isinstance(body, dict):
        raise ApiError(ErrCode.PARAM_INVALID, "请求体必须是 JSON 对象")
    return body


def body_str(body: dict, key: str, *, required: bool = False, default: Any = None,
             max_len: int = 200) -> Any:
    value = body.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要字段：{key}")
        return default
    text = str(value).strip()
    if len(text) > max_len:
        raise ApiError(ErrCode.PARAM_INVALID, f"字段 {key} 超长")
    return text


def body_int(body: dict, key: str, *, required: bool = False, default: int | None = None,
             minimum: int | None = None, maximum: int | None = None) -> int | None:
    value = body.get(key)
    if value is None or value == "":
        if required:
            raise ApiError(ErrCode.PARAM_MISSING, f"缺少必要字段：{key}")
        return default
    try:
        number = Decimal(str(value))
        if isinstance(value, bool) or not number.is_finite() or number != number.to_integral_value():
            raise ValueError
        out = int(number)
    except (TypeError, ValueError, OverflowError, InvalidOperation):
        raise ApiError(ErrCode.PARAM_INVALID, f"字段 {key} 必须是整数")
    if minimum is not None and out < minimum:
        raise ApiError(ErrCode.PARAM_INVALID, f"字段 {key} 不能小于 {minimum}")
    if maximum is not None and out > maximum:
        raise ApiError(ErrCode.PARAM_INVALID, f"字段 {key} 不能大于 {maximum}")
    return out
