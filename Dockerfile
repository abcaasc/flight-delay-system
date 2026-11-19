# syntax=docker/dockerfile:1

# ============================================================
#  单容器部署：前端构建 → 拷进后端镜像 → 后端同时托管网页与 API
#  构建（仓库根目录）：docker build -t flight-delay-system .
#  运行：docker run -p 7860:7860 flight-delay-system
# ============================================================

# ---------- 阶段一：构建前端 ----------
FROM node:22-slim AS frontend

WORKDIR /web

# 先只拷依赖清单，利用层缓存：源码改动不会重装 node_modules
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build

# ---------- 阶段二：后端运行时 ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FLASK_ENV=prod \
    HOST=0.0.0.0 \
    PORT=7860 \
    QUERY_ENGINE=pandas \
    FRONTEND_DIST=/web \
    WAREHOUSE_ROOT=/app/data/demo/warehouse \
    MODEL_ROOT=/app/data/models \
    STAGING_ROOT=/app/data/staging \
    DATA_ROOT=/app/data/source

WORKDIR /app

# libgomp1：LightGBM 的 OpenMP 运行时，slim 镜像里没有，
# 缺了会让 `import lightgbm` 抛 OSError: libgomp.so.1 并静默降级成基线模型
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 依赖单独一层，改代码不必重装依赖
# requirements-demo.txt = requirements.txt + lightgbm/scikit-learn，
# 这样容器里跑的是训练好的 LightGBM 模型，而不是降级基线
COPY backend/requirements.txt backend/requirements-demo.txt ./
RUN pip install --no-cache-dir -r requirements-demo.txt

COPY backend/ ./
COPY --from=frontend /web/dist /web

EXPOSE 7860

# 说明：生产环境 run.py 走 waitress（多线程 WSGI），不启用 Flask 调试器。
# 启动时：① 没给 SECRET_KEY 就随机生成（重启后旧 token 失效，演示场景无影响）
#        ② 仅在本地显式设置 SEED_DEMO_USERS=1 时初始化演示账号
#        ③ 起服务
CMD ["sh", "-c", "export SECRET_KEY=\"${SECRET_KEY:-$(python -c 'import secrets;print(secrets.token_hex(32))')}\"; if [ \"${SEED_DEMO_USERS:-0}\" = 1 ]; then python -m flask --app run.py seed-users || echo '[warn] 演示账号初始化跳过'; fi; exec python run.py"]
