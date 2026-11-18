"""认证与 RBAC 授权（对应 §6.3 用户角色与权限）。

实现取舍（§6.3.3）：JWT(HS256) 无状态认证 + 装饰器角色校验，预置 4 个账号，
不做注册/找回密码/多租户。角色为偏序：guest ⊂ ops ⊂ analyst ⊂ admin，
@require_role("analyst") 表示"analyst 及以上"。
"""
from __future__ import annotations

import functools
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Callable

import jwt
from flask import current_app, g, request

from app.config import Config
from app.utils.response import ApiError, ErrCode

ROLE_RANK = {"guest": 1, "ops": 2, "analyst": 3, "admin": 4}
ROLE_NAMES = {
    "guest": "访客/评审",
    "ops": "运行监控员",
    "analyst": "数据分析员",
    "admin": "系统管理员",
}

# 菜单可见性（§6.4 菜单按角色动态渲染，guest 只见"态势监控"）
ROLE_MENUS = {
    "guest": ["dashboard"],
    "ops": ["dashboard", "analysis"],
    "analyst": ["dashboard", "analysis", "model"],
    "admin": ["dashboard", "analysis", "model", "data", "system"],
}


# ---------------------------------------------------------------- 口令哈希
def hash_password(password: str, salt: str | None = None) -> str:
    """PBKDF2-HMAC-SHA256 加盐哈希，格式：pbkdf2$迭代$salt$hash。"""
    salt = salt or secrets.token_hex(16)
    iterations = 120_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2${iterations}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(iterations))
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


# ---------------------------------------------------------------- JWT
def issue_token(user) -> tuple[str, int]:
    expires_in = Config.JWT_TTL_HOURS * 3600
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "username": user.username,
        "role": user.role,
        "iss": Config.JWT_ISSUER,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
    }
    token = jwt.encode(payload, Config.JWT_SECRET, algorithm=Config.JWT_ALGORITHM)
    return token, expires_in


def decode_token(token: str) -> dict:
    try:
        claims = jwt.decode(token, Config.JWT_SECRET, algorithms=[Config.JWT_ALGORITHM],
                            issuer=Config.JWT_ISSUER,
                            options={"require": ["sub", "exp", "iat", "iss"]})
        if not isinstance(claims["sub"], str) or not claims["sub"].isdigit():
            raise jwt.InvalidTokenError("Invalid subject")
        return claims
    except jwt.ExpiredSignatureError:
        raise ApiError(ErrCode.UNAUTHORIZED, "登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise ApiError(ErrCode.UNAUTHORIZED, "无效的访问令牌")


def _extract_token() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    # 导出/下载场景允许 query 传 token（浏览器直接点链接）
    return request.args.get("token")


def load_current_user(required: bool = True):
    """解析当前请求的用户，缓存在 g。"""
    if getattr(g, "user", None) is not None:
        return g.user
    if getattr(g, "anonymous", False) and not required:
        return None

    from app.extensions import db  # 延迟导入，避免循环依赖
    from app.models import User

    token = _extract_token()
    if not token:
        if required:
            raise ApiError(ErrCode.UNAUTHORIZED)
        g.anonymous = True
        return None
    claims = decode_token(token)
    user = db.session.query(User).filter_by(id=int(claims["sub"])).first()
    if not user or not user.is_active:
        raise ApiError(ErrCode.UNAUTHORIZED, "账号不存在或已停用")
    g.user = user
    g.claims = claims
    return user


def current_user():
    return getattr(g, "user", None)


def anonymous_view() -> dict:
    return {"id": 0, "username": "guest", "role": "guest", "display_name": ROLE_NAMES["guest"]}


def current_identity() -> dict:
    user = current_user()
    if user is None:
        return anonymous_view()
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "display_name": user.display_name or ROLE_NAMES.get(user.role, user.role),
    }


def role_rank(role: str | None) -> int:
    return ROLE_RANK.get((role or "guest").lower(), 0)


# ---------------------------------------------------------------- 装饰器
def login_required(fn: Callable) -> Callable:
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        load_current_user(required=True)
        return fn(*args, **kwargs)

    return wrapper


def require_role(min_role: str) -> Callable:
    """要求"至少 min_role"；guest 可读的接口不要加此装饰器。"""
    threshold = role_rank(min_role)
    if threshold <= 0:
        raise ValueError(f"未知角色：{min_role}")

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            user = load_current_user(required=True)
            if role_rank(user.role) < threshold:
                raise ApiError(
                    ErrCode.FORBIDDEN,
                    f"当前角色（{ROLE_NAMES.get(user.role, user.role)}）无权访问，"
                    f"需要 {ROLE_NAMES.get(min_role, min_role)} 及以上权限",
                )
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def optional_auth(fn: Callable) -> Callable:
    """允许匿名访问（guest 只读接口）。"""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        load_current_user(required=False)
        return fn(*args, **kwargs)

    return wrapper


def write_audit(action: str, resource: str, detail: str = "", ok: bool = True) -> None:
    """审计留痕（§6.3.2 审计日志 / §3.1.6）。"""
    from app.extensions import db
    from app.models import AuditLog

    try:
        user = current_identity()
        row = AuditLog(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            username=user["username"],
            role=user["role"],
            action=action,
            resource=resource,
            detail=(detail or "")[:2000],
            ip=(request.headers.get("X-Forwarded-For") or request.remote_addr or "")[:64],
            trace_id=getattr(g, "trace_id", ""),
            result="success" if ok else "failed",
        )
        db.session.add(row)
        db.session.commit()
    except Exception:  # 审计失败不影响主流程
        db.session.rollback()
        current_app.logger.warning("写审计日志失败", exc_info=True)
