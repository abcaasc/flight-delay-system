"""pytest 公共夹具：独立临时数仓 + 临时 SQLite，不污染开发数据。"""
from __future__ import annotations

import os
import shutil
import tempfile

import pytest

# ⚠ 必须在导入 app 之前设置环境变量（Config 在 import 时读取）
_TMP = tempfile.mkdtemp(prefix="fds-test-")
os.environ["WAREHOUSE_ROOT"] = os.path.join(_TMP, "warehouse")
os.environ["STAGING_ROOT"] = os.path.join(_TMP, "staging")
os.environ["MODEL_ROOT"] = os.path.join(_TMP, "models")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["QUERY_ENGINE"] = "pandas"
os.environ["REDIS_URL"] = ""
os.environ["FLASK_ENV"] = "test"


@pytest.fixture(scope="session")
def app():
    from app import create_app
    from app.extensions import db
    from app.models import User
    from app.utils.auth import hash_password

    application = create_app()
    with application.app_context():
        for username, password, role in (("guest", "guest123", "guest"),
                                         ("ops", "ops123", "ops"),
                                         ("analyst", "analyst123", "analyst"),
                                         ("admin", "admin123", "admin")):
            if not db.session.query(User).filter_by(username=username).first():
                db.session.add(User(username=username, password_hash=hash_password(password),
                                    role=role, display_name=username))
        db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def tokens(app, client):
    out = {}
    for username, password in (("guest", "guest123"), ("ops", "ops123"),
                               ("analyst", "analyst123"), ("admin", "admin123")):
        response = client.post("/api/v1/auth/login",
                               json={"username": username, "password": password})
        payload = response.get_json()
        out[username] = payload["data"]["token"] if payload.get("code") == 0 else None
    return out


@pytest.fixture()
def auth(tokens):
    def _headers(role: str = "admin") -> dict:
        token = tokens.get(role)
        return {"Authorization": f"Bearer {token}"} if token else {}

    return _headers


def pytest_sessionfinish(session, exitstatus):
    """测试结束后清理临时目录（失败时保留便于排查）。"""
    if exitstatus == 0:
        shutil.rmtree(_TMP, ignore_errors=True)

