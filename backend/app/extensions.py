"""Flask 扩展单例（避免循环导入）。

不引入 Flask-SQLAlchemy，直接用纯 SQLAlchemy 2.0 —— 依赖更少、事务边界更清楚。
"""
from __future__ import annotations

from flask_cors import CORS
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from app.config import Config

_connect_args = {"check_same_thread": False} if Config.DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    Config.DATABASE_URL,
    echo=False,
    future=True,
    pool_pre_ping=True,
    connect_args=_connect_args,
)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))


class _DB:
    """极简 db 门面：db.session.add(...) / db.session.commit() / db.session.query(...)。"""

    engine = engine
    session = SessionLocal

    @staticmethod
    def create_all() -> None:
        from app.models import Base

        Base.metadata.create_all(bind=engine)

    @staticmethod
    def drop_all() -> None:
        from app.models import Base

        Base.metadata.drop_all(bind=engine)


db = _DB()

cors = CORS()
