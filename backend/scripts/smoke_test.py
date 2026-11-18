"""接口冒烟测试：逐一调用全部主要接口，校验响应结构与业务约束。

用法：
    python scripts/smoke_test.py                 # 默认 http://127.0.0.1:5000
    python scripts/smoke_test.py --base http://127.0.0.1:5055

覆盖：认证/权限、看板、网络、排行、传播、查询导出、预测归因、模型、数据管理、系统管理、AI。
"""
from __future__ import annotations

import argparse
import json
import sys

import requests

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, method: str, path: str, *, base: str, token: str | None = None,
          expect_code: int = 0, expect_http: int | tuple[int, ...] = (200,),
          body: dict | None = None, params: dict | None = None, note: str = "") -> dict | None:
    url = f"{base}{path}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = requests.request(method, url, headers=headers, json=body, params=params,
                                    timeout=180)
    except Exception as exc:
        results.append((name, FAIL, f"请求异常 {exc}"))
        return None
    expected = (expect_http,) if isinstance(expect_http, int) else expect_http
    try:
        payload = response.json()
    except Exception:
        payload = {}
    code = payload.get("code")
    ok = response.status_code in expected and code == expect_code
    detail = f"HTTP {response.status_code} code={code}"
    if not ok:
        detail += f" msg={payload.get('message') or payload.get('detail')}"
    results.append((name, PASS if ok else FAIL, detail + (f" | {note}" if note else "")))
    return payload.get("data") if ok else payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:5000")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print(f"目标服务：{base}\n")

    # ---------------- 认证 ----------------
    data = check("登录 admin", "POST", "/api/v1/auth/login", base=base,
                 body={"username": "admin", "password": "admin123"})
    admin_token = (data or {}).get("token")
    check("登录 guest", "POST", "/api/v1/auth/login", base=base,
          body={"username": "guest", "password": "guest123"})
    guest = check("再次登录 guest 取 token", "POST", "/api/v1/auth/login", base=base,
                  body={"username": "guest", "password": "guest123"})
    guest_token = (guest or {}).get("token")
    check("错误口令被拒", "POST", "/api/v1/auth/login", base=base,
          body={"username": "admin", "password": "wrong"}, expect_code=401, expect_http=401)
    check("未带 token 访问 profile", "GET", "/api/v1/auth/profile", base=base,
          expect_code=401, expect_http=401)
    check("admin profile", "GET", "/api/v1/auth/profile", base=base, token=admin_token)
    check("guest 无权看传播路径(U-02)", "GET", "/api/v1/propagation", base=base,
          token=guest_token, expect_code=403, expect_http=403)

    # ---------------- 看板 ----------------
    kpi = check("P1 总览 KPI", "GET", "/api/v1/overview/kpi", base=base, token=guest_token)
    if isinstance(kpi, dict) and "kpi" in kpi:
        print(f"   KPI：航班 {kpi['kpi']['flights']}，准点率 {kpi['kpi']['ontime_rate']}，"
              f"平均延误 {kpi['kpi']['avg_delay_min']} 分钟")
    check("P1 趋势(day)", "GET", "/api/v1/overview/trend", base=base, token=guest_token,
          params={"granularity": "day"})
    check("P1 延误分布", "GET", "/api/v1/overview/distribution", base=base, token=guest_token)
    check("P1 延误原因分解", "GET", "/api/v1/overview/reasons", base=base, token=guest_token)
    check("非法参数被拒(月份格式)", "GET", "/api/v1/overview/kpi", base=base, token=guest_token,
          params={"date_from": "2015-1-1"}, expect_code=422, expect_http=400)
    net = check("P2 航线网络", "GET", "/api/v1/network", base=base, token=guest_token)
    if isinstance(net, dict) and "summary" in net:
        print(f"   网络：{net['summary']['airports']} 机场 / {net['summary']['routes']} 航线")
    check("P2 热力图", "GET", "/api/v1/network/heatmap", base=base, token=guest_token,
          params={"top": 10})
    rank = check("P3 机场排行", "GET", "/api/v1/airport/rank", base=base, token=guest_token,
                 params={"metric": "delay_rate", "top": 5})
    if isinstance(rank, dict) and rank.get("items"):
        print(f"   延误率 Top1：{rank['items'][0]['airport_iata']} {rank['items'][0]['value']}")
    check("P3 机场详情", "GET", "/api/v1/airport/ATL", base=base, token=guest_token)
    check("P3 机场航线下钻", "GET", "/api/v1/airport/ATL/rank", base=base, token=guest_token)
    check("P3 机场代码非法", "GET", "/api/v1/airport/AT", base=base, token=guest_token,
          expect_code=422, expect_http=400)
    check("P4 延误传播(ops)", "GET", "/api/v1/propagation", base=base, token=admin_token)

    # ---------------- 查询与导出 ----------------
    flights = check("查询航班(分页)", "GET", "/api/v1/flights", base=base, token=guest_token,
                    params={"origin": "JFK", "page": 1, "page_size": 5})
    if isinstance(flights, dict) and flights.get("items") is not None:
        print(f"   命中 {flights['total']} 条，返回 {len(flights['items'])} 条")
        first_sk = flights["items"][0]["flight_sk"] if flights["items"] else None
    else:
        first_sk = None
    check("航班详情", "GET", f"/api/v1/flights/{first_sk}" if first_sk else "/api/v1/flights/x",
          base=base, token=guest_token, expect_code=0 if first_sk else 404,
          expect_http=(200,))
    for label, path in (("导出航班 CSV", "/api/v1/flights/export"),
                        ("导出总览 CSV", "/api/v1/overview/export")):
        response = requests.get(f"{base}{path}",
                                headers={"Authorization": f"Bearer {guest_token}"}, timeout=180)
        is_csv = "text/csv" in (response.headers.get("content-type") or "")
        max_rows = response.headers.get("X-Max-Rows")
        ok = response.status_code == 200 and is_csv and max_rows == "1000"
        results.append((label, PASS if ok else FAIL,
                        f"HTTP {response.status_code} {len(response.content)}B 上限={max_rows}"))

    # ---------------- 预测与归因 ----------------
    pred = check("单航班预测", "POST", "/api/v1/predict", base=base, token=guest_token,
                 body={"origin": "JFK", "dest": "LAX", "airline": "AA",
                       "flight_date": "2015-01-15", "crs_dep_time": "0900"})
    if isinstance(pred, dict) and "prediction" in pred:
        print(f"   概率 {pred['prediction']['prob_delay']}，等级 {pred['prediction']['risk_level']}，"
              f"Top1 因素 {pred['explanation']['factors'][0]['feature']}")
    check("预测参数非法(同时刻同机场)", "POST", "/api/v1/predict", base=base, token=guest_token,
          body={"origin": "JFK", "dest": "JFK", "airline": "AA",
                "flight_date": "2015-01-15", "crs_dep_time": "0900"},
          expect_code=422, expect_http=400)
    check("高风险航班", "GET", "/api/v1/predict/high-risk", base=base, token=guest_token,
          params={"top": 5})
    check("机场级预测聚合", "GET", "/api/v1/predict/airport", base=base, token=guest_token)
    check("全局特征重要度", "GET", "/api/v1/explain/global", base=base, token=guest_token)
    if first_sk:
        check("单航班 SHAP 归因(P7)", "GET", f"/api/v1/explain/{first_sk}", base=base,
              token=admin_token)
    check("归因-航班不存在", "GET", "/api/v1/explain/not-a-flight", base=base,
          token=admin_token, expect_code=404, expect_http=404)

    # ---------------- 模型中心 ----------------
    versions = check("模型版本列表", "GET", "/api/v1/model/versions", base=base, token=admin_token)
    check("模型对比(P5)", "GET", "/api/v1/model/compare", base=base, token=admin_token,
          params={"task": "cls15"})
    check("泛化矩阵(P6)", "GET", "/api/v1/model/generalization", base=base, token=admin_token)
    spec = check("特征白名单/泄漏检查(A-8)", "GET", "/api/v1/model/feature-spec", base=base,
                 token=admin_token)
    if isinstance(spec, dict) and "count" in spec:
        print(f"   特征 {spec['count']} 个，泄漏检查：{spec['leakage_check']}")
    check("训练(analyst 权限不足→admin 可)", "POST", "/api/v1/model/train", base=base,
          token=guest_token, body={"task": "cls15", "periods": ["2015-01"]},
          expect_code=403, expect_http=403)
    if versions and versions.get("items"):
        ver = versions["items"][0]["model_ver"]
        check("模型上线(admin)", "POST", "/api/v1/model/online", base=base, token=admin_token,
              body={"model_ver": ver})

    # ---------------- 数据管理 ----------------
    ds = check("数据集清单", "GET", "/api/v1/ingest/datasets", base=base, token=admin_token)
    if isinstance(ds, dict) and ds.get("items"):
        ready = [i["key"] for i in ds["items"] if i["ready"]]
        print(f"   数据集就绪：{len(ready)}/9 {ready}")
    check("数据集文件清单", "GET", "/api/v1/ingest/datasets/bts/files", base=base,
          token=admin_token, params={"page_size": 5})
    check("作业列表", "GET", "/api/v1/ingest/jobs", base=base, token=admin_token)
    check("作业详情(不存在)", "GET", "/api/v1/ingest/jobs/nope", base=base, token=admin_token,
          expect_code=404, expect_http=404)
    check("校验(不存在的月份被拦截 U-13)", "POST", "/api/v1/ingest/verify", base=base,
          token=admin_token, body={"dataset": "bts", "periods": ["2024-13"]},
          expect_code=422, expect_http=400)
    check("创建作业 dry_run", "POST", "/api/v1/ingest/jobs", base=base, token=admin_token,
          body={"job_type": "etl", "dataset": "bts", "periods": ["2015-01"], "dry_run": True})
    check("死信列表", "GET", "/api/v1/ingest/failed", base=base, token=admin_token)
    check("质量报告", "GET", "/api/v1/quality/report", base=base, token=admin_token)
    tables = check("数仓表浏览", "GET", "/api/v1/warehouse/tables", base=base, token=admin_token)
    if isinstance(tables, dict) and tables.get("items"):
        ready = sum(1 for i in tables["items"] if i["ready"])
        print(f"   数仓：{ready}/{len(tables['items'])} 张表就绪，引擎 {tables['engine']}")
    check("分区详情", "GET", "/api/v1/warehouse/tables/dwd_flight_detail/partitions", base=base,
          token=admin_token)
    # 真实的 multipart 上传：.exe 必须被拒（白名单校验）
    try:
        files = {"file": ("evil.exe", b"MZ\x90\x00fake", "application/octet-stream")}
        response = requests.post(f"{base}/api/v1/upload/staging",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 files=files, data={"dataset": "custom"}, timeout=60)
        payload = response.json()
        ok = payload.get("code") == 415
        results.append(("上传 .exe 被拒(U-12)", PASS if ok else FAIL,
                        f"HTTP {response.status_code} code={payload.get('code')}"))
    except Exception as exc:
        results.append(("上传 .exe 被拒(U-12)", FAIL, f"请求异常 {exc}"))
    # 合法小 CSV 应被接收
    try:
        files = {"file": ("holiday_2024.csv", "date,name\n2024-01-01,New Year\n".encode(),
                          "text/csv")}
        response = requests.post(f"{base}/api/v1/upload/staging",
                                 headers={"Authorization": f"Bearer {admin_token}"},
                                 files=files, data={"dataset": "custom"}, timeout=60)
        payload = response.json()
        ok = payload.get("code") == 0 and payload["data"]["file_bytes"] > 0
        results.append(("上传合法 CSV 到 staging", PASS if ok else FAIL,
                        f"HTTP {response.status_code} code={payload.get('code')}"))
    except Exception as exc:
        results.append(("上传合法 CSV 到 staging", FAIL, f"请求异常 {exc}"))

    # ---------------- 系统管理 ----------------
    check("健康检查", "GET", "/api/v1/system/health", base=base)
    check("数据新鲜度", "GET", "/api/v1/system/freshness", base=base, token=guest_token)
    check("作业监控(P8)", "GET", "/api/v1/system/job/stats", base=base, token=admin_token)
    check("审计日志", "GET", "/api/v1/system/audit", base=base, token=admin_token,
          params={"page_size": 5})
    check("用户列表", "GET", "/api/v1/system/users", base=base, token=admin_token)
    check("告警规则", "GET", "/api/v1/system/alerts/rules", base=base, token=admin_token)
    check("告警事件", "GET", "/api/v1/system/alerts/events", base=base, token=admin_token)
    check("缓存统计/清理", "POST", "/api/v1/system/cache/flush", base=base, token=admin_token)
    check("接口清单", "GET", "/api/v1/system/endpoints", base=base)

    # ---------------- 跨域与 AI ----------------
    check("中国域指标(STPN)", "GET", "/api/v1/crossdomain/china", base=base, token=guest_token,
          params={"top": 5})
    check("中美跨域对照", "GET", "/api/v1/crossdomain/compare", base=base, token=guest_token)
    ai = check("AI 智能问答", "POST", "/api/v1/ai/chat", base=base, token=guest_token,
               body={"question": "本月准点率怎么样？哪些机场最差？"})
    if isinstance(ai, dict) and ai.get("answer"):
        print(f"   AI 回答（degraded={ai['degraded']}）：{ai['answer'][:80]}…")

    # ---------------- 汇总 ----------------
    total = len(results)
    passed = sum(1 for _, s, _ in results if s == PASS)
    failed = [r for r in results if r[1] == FAIL]
    print("\n" + "=" * 78)
    for name, status, detail in results:
        mark = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭"}[status]
        print(f"{mark} {name:<38s} {detail}")
    print("=" * 78)
    print(f"合计 {total} 项，通过 {passed}，失败 {len(failed)}")
    if failed:
        print("\n失败项：")
        for name, _, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


