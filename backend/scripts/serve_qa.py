"""用独立 SQLite 和测试账号启动浏览器回归环境。"""
import argparse
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=["warehouse", "demo"])
    parser.add_argument("--port", type=int, default=5173)
    parser.add_argument("--warehouse", type=Path, help="替换默认数仓目录")
    parser.add_argument("--frontend", type=Path, default=BACKEND.parent / "frontend" / "dist")
    args = parser.parse_args()
    warehouse = args.warehouse or BACKEND / "data" / (
        "warehouse" if args.profile == "warehouse" else "demo/warehouse")
    if not warehouse.is_dir() or not (args.frontend / "index.html").is_file():
        parser.error("需要有效的数仓目录和前端构建产物，请先执行 npm run build")

    instance = BACKEND / "instance"
    instance.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="browser-qa-", dir=instance))
    os.environ.update(
        FLASK_ENV="test", QUERY_ENGINE="pandas", REDIS_URL="", DEEPSEEK_API_KEY="",
        WAREHOUSE_ROOT=str(warehouse.resolve()), FRONTEND_DIST=str(args.frontend.resolve()),
        MODEL_ROOT=str(BACKEND / "data" / "models"), STAGING_ROOT=str(root / "staging"),
        JWT_SECRET=secrets.token_hex(32), DATABASE_URL="sqlite:///" + (root / "qa.db").as_posix(),
        ROLLING_BACKTEST_REPORT=str(root / "rolling-backtest.json"),
        FORM_MODEL_REPORT=str(root / "form-model-validation.json"),
    )
    if args.profile == "demo":
        os.environ["DATA_ROOT"] = str(root / "no-sources")

    from app import create_app
    from app.extensions import db
    from app.models import JobRun, ModelVersion, User
    from app.services import model
    from app.utils.auth import hash_password
    from waitress import serve

    app = create_app()
    if args.profile == "warehouse":
        with app.app_context():
            for role in ("admin", "ops", "analyst", "guest"):
                db.session.add(User(username=role, password_hash=hash_password(role + "123"), role=role))
            db.session.add(JobRun(job_id="qa-2015-01", job_name="回归测试作业", dataset="bts",
                                  period="2015-01", status="success"))
            active = model.online_model()
            if active:
                db.session.add(ModelVersion(model_ver=active["meta"]["model_ver"], task="cls15",
                                            artifact_path=Path(active["meta"]["artifact_path"]).name, is_online=1))
            metrics = {"support": 20, "auc": 0.7, "recall": 0.6, "precision": 0.5, "f1": 0.55, "threshold": 0.4}
            pair = {"full": metrics, "no_chain": metrics}
            report = {"model_ver": active["meta"]["model_ver"] if active else model.baseline_version(),
                      "holdout": {"test_range": ["2024-12-18", "2024-12-31"], **pair},
                      "folds": [{"train": ["2024-08", "2024-09"], "validation": "2024-10",
                                 "test": "2024-11", **pair},
                                {"train": ["2024-09", "2024-10"], "validation": "2024-11",
                                 "test": "2024-12", **pair}]}
            (root / "rolling-backtest.json").write_text(json.dumps(report), encoding="utf-8")
            db.session.commit()
    serve(app, host="127.0.0.1", port=args.port, threads=6)


if __name__ == "__main__":
    main()
