"""认证接口：登录换 JWT、当前用户、修改口令。"""
from __future__ import annotations

from datetime import datetime

from flask import Blueprint, g, request

from app.extensions import db
from app.models import User
from app.utils import params
from app.utils.auth import (
    ROLE_MENUS, ROLE_NAMES, current_identity, hash_password, issue_token,
    load_current_user, verify_password, write_audit,
)
from app.utils.response import ApiError, ErrCode, ok

bp = Blueprint("auth", __name__, url_prefix="/api/v1/auth")


@bp.post("/login")
def login():
    body = params.json_body()
    username = params.body_str(body, "username", required=True, max_len=64)
    password = params.body_str(body, "password", required=True, max_len=128)

    user = db.session.query(User).filter_by(username=username).first()
    if user is None or not verify_password(password, user.password_hash):
        write_audit("login", "sys_user", f"用户 {username} 登录失败", ok=False)
        raise ApiError(ErrCode.UNAUTHORIZED, "用户名或口令不正确")
    if not user.is_active:
        raise ApiError(ErrCode.FORBIDDEN, "账号已停用，请联系管理员")

    token, expires_in = issue_token(user)
    user.last_login_at = datetime.now()
    db.session.commit()
    write_audit("login", "sys_user", f"用户 {username} 登录成功")

    return ok({
        "token": token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "user": user.to_dict(),
        "role_name": ROLE_NAMES.get(user.role, user.role),
        "menus": ROLE_MENUS.get(user.role, []),
    })


@bp.get("/profile")
def profile():
    user = load_current_user(required=True)
    return ok({
        "user": user.to_dict(),
        "role_name": ROLE_NAMES.get(user.role, user.role),
        "menus": ROLE_MENUS.get(user.role, []),
        "permissions": _permission_summary(user.role),
    })


@bp.post("/password")
def change_password():
    user = load_current_user(required=True)
    body = params.json_body()
    old = params.body_str(body, "old_password", required=True)
    new = params.body_str(body, "new_password", required=True, max_len=128)
    if not verify_password(old, user.password_hash):
        raise ApiError(ErrCode.PARAM_INVALID, "原口令不正确")
    if len(new) < 6:
        raise ApiError(ErrCode.PARAM_INVALID, "新口令至少 6 位")
    user.password_hash = hash_password(new)
    db.session.commit()
    write_audit("change_password", "sys_user", f"用户 {user.username} 修改口令")
    return ok({"username": user.username, "changed": True})


@bp.post("/logout")
def logout():
    identity = current_identity()
    write_audit("logout", "sys_user", f"用户 {identity['username']} 退出登录")
    # 无状态 JWT：服务端不维护黑名单，前端丢弃 token 即可
    return ok({"logged_out": True, "username": identity["username"]})


def _permission_summary(role: str) -> dict:
    from app.utils.auth import role_rank

    return {
        "role": role,
        "rank": role_rank(role),
        "can_write_model": role_rank(role) >= role_rank("analyst"),
        "can_manage_data": role_rank(role) >= role_rank("admin"),
        "can_manage_user": role_rank(role) >= role_rank("admin"),
        "read_only": role == "guest",
    }
