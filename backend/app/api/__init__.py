"""接口蓝图注册。所有蓝图统一挂在 /api/v1 下（§6.2 RESTful 版本前缀）。"""
from __future__ import annotations

from flask import Flask

from app.api import ai, auth, dashboard, data, flights, model_api, predict, system

BLUEPRINTS = (
    auth.bp,
    dashboard.bp,
    flights.bp,
    predict.bp,
    model_api.bp,
    data.bp,
    system.bp,
    ai.bp,
)


def register_blueprints(app: Flask) -> None:
    for bp in BLUEPRINTS:
        app.register_blueprint(bp)


__all__ = ["register_blueprints", "BLUEPRINTS"]
