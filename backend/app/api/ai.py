"""智能问答（AI 分析助手）：DeepSeek API + 本地统计摘要降级。

开题报告「AI 工具使用计划：计划使用 Deepseek API 实现 AI 分析助手模块」。
未配置 DEEPSEEK_API_KEY 时不允许白屏：自动用同一份统计上下文生成模板化解读，
并在响应里标记 degraded=true（页面顶部显示降级标记）。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime

from flask import Blueprint

from app.config import Config
from app.extensions import db
from app.models import AiChatLog
from app.services import stats
from app.utils import params
from app.utils.auth import current_identity, optional_auth
from app.utils.logger import get_logger
from app.utils.response import ApiError, ErrCode, ok

log = get_logger(__name__)
bp = Blueprint("ai", __name__, url_prefix="/api/v1/ai")

SYSTEM_PROMPT = """你是"航班延误预测与航线网络态势可视化系统"的分析助手。
规则：
1. 只根据给出的统计上下文回答，不要编造数字；上下文没有的就明确说"当前数据未覆盖"。
2. 指标口径：准点率 = 实际执行航班中起飞延误 < 15 分钟的占比（分母不含取消）；延误率 = 延误航班 / 实际执行；取消率 = 取消 / 计划航班。
3. 回答用中文，先给结论（1–2 句），再给 2–4 条依据，最后如有必要给出 1 条可执行的下一步建议。
4. 不要输出免责声明，不要复述问题。"""


def _context(question: str, date_from: str | None, date_to: str | None) -> dict:
    """装配统计上下文：总览 KPI + 延误率最高的机场 + 航线概况。"""
    context: dict = {"question": question}
    try:
        kpi = stats.overview_kpi(date_from, date_to, (), ())
        context["overview"] = kpi["kpi"]
        context["window"] = kpi["period"]
        context["previous"] = kpi.get("compared_to_previous", {})
    except ApiError as exc:
        context["overview"] = {"error": exc.message}
    try:
        rank = stats.airport_rank(None, "delay_rate", 5, "desc")
        context["top_delay_airports"] = rank["items"]
    except ApiError:
        context["top_delay_airports"] = []
    try:
        dist = stats.delay_distribution(date_from, date_to, ())
        context["delay_distribution"] = dist["buckets"]
    except ApiError:
        pass
    try:
        net = stats.network_graph(date_from, date_to, "delay_rate", 5, 1)
        context["network_summary"] = net["summary"]
    except ApiError:
        pass
    return context


def _local_answer(context: dict) -> str:
    """无 API Key 时的模板化解读：仍然基于真实统计数字。"""
    kpi = context.get("overview", {})
    if "error" in kpi:
        return f"当前无法读取统计上下文：{kpi['error']}。请先在数据管理中完成数据接入与 ETL。"
    lines = []
    window = context.get("window", {})
    lines.append(
        f"在 {window.get('date_from', '—')} 至 {window.get('date_to', '—')} 的统计期内，"
        f"共 {kpi.get('flights', 0):,} 个计划航班，准点率 "
        f"{(kpi.get('ontime_rate') or 0) * 100:.2f}%，平均起飞延误 "
        f"{kpi.get('avg_delay_min', 0)} 分钟，取消率 {(kpi.get('cancel_rate') or 0) * 100:.2f}%。"
    )
    prev = context.get("previous") or {}
    if prev.get("ontime_rate") is not None and kpi.get("ontime_rate") is not None:
        delta = (kpi["ontime_rate"] - prev["ontime_rate"]) * 100
        direction = "上升" if delta >= 0 else "下降"
        lines.append(f"较上一等长区间（{prev.get('window', {}).get('date_from')}"
                     f"~{prev.get('window', {}).get('date_to')}）准点率{direction} "
                     f"{abs(delta):.2f} 个百分点。")
    top = context.get("top_delay_airports") or []
    if top:
        names = "、".join(
            f"{i['airport_iata']}（{(i['value'] or 0) * 100:.1f}%）" for i in top[:3])
        lines.append(f"延误率最高的机场依次为：{names}，可作为运力调配与预警的重点监控对象。")
    dist = context.get("delay_distribution") or []
    if dist:
        worst = max(dist, key=lambda x: x["count"])
        lines.append(f"延误时长分布中最集中的区间是「{worst['label']}」，"
                     f"占 {worst['pct']}%，说明当前延误以短时延误为主。")
    net = context.get("network_summary") or {}
    if net:
        lines.append(f"网络侧覆盖 {net.get('airports')} 个机场、{net.get('routes')} 条航线，"
                     f"平均延误率 {(net.get('avg_delay_rate') or 0) * 100:.2f}%。")
    lines.append("下一步建议：对 Top3 机场下钻 24 小时曲线并叠加气象，确认是否为天气主导型延误。")
    return "\n".join(lines)


def _call_deepseek(question: str, context: dict) -> str:
    payload = {
        "model": Config.DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "统计上下文（JSON）：\n"
                                        + json.dumps(context, ensure_ascii=False, default=str)
                                        + f"\n\n用户问题：{question}"},
        ],
        "temperature": 0.3,
        "max_tokens": 900,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{Config.DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {Config.DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=Config.DEEPSEEK_TIMEOUT) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


@bp.post("/chat")
@optional_auth
def chat():
    body = params.json_body()
    question = params.body_str(body, "question", required=True, max_len=1000)
    date_from = params.body_str(body, "date_from", default=None, max_len=10)
    date_to = params.body_str(body, "date_to", default=None, max_len=10)

    context = _context(question, date_from, date_to)
    started = time.time()
    degraded = 0
    model_name = Config.DEEPSEEK_MODEL

    if Config.DEEPSEEK_API_KEY:
        try:
            answer = _call_deepseek(question, context)
        except (urllib.error.URLError, urllib.error.HTTPError, KeyError, TimeoutError) as exc:
            log.warning("DeepSeek 调用失败，降级为本地摘要：%s", exc)
            answer = _local_answer(context)
            degraded = 1
            model_name = "local-summary"
    else:
        answer = _local_answer(context)
        degraded = 1
        model_name = "local-summary"

    latency = int((time.time() - started) * 1000)
    try:
        db.session.add(AiChatLog(
            ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            username=current_identity()["username"], question=question[:4000],
            answer=answer, context_json=json.dumps(context, ensure_ascii=False, default=str)[:20000],
            model=model_name, degraded=degraded, latency_ms=latency,
        ))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        log.warning("写 AI 会话日志失败：%s", exc)

    return ok({
        "question": question,
        "answer": answer,
        "degraded": bool(degraded),
        "model": model_name,
        "latency_ms": latency,
        "context_keys": list(context.keys()),
        "note": ("未配置 DEEPSEEK_API_KEY，已使用本地统计摘要生成答案"
                 if degraded and not Config.DEEPSEEK_API_KEY else
                 "调用外部模型失败，已降级" if degraded else "DeepSeek 已生成"),
    })


@bp.get("/history")
@optional_auth
def history():
    rows = db.session.query(AiChatLog).order_by(AiChatLog.id.desc()).limit(
        params.get_int("limit", 20, minimum=1, maximum=100)).all()
    return ok({"items": [r.to_dict() for r in rows]})
