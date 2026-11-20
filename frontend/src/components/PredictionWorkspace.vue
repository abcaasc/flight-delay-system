<script setup lang="ts">
import { onBeforeUnmount, reactive, ref, watch } from "vue";
import { ArrowRight, BrainCircuit, PlaneTakeoff, ScanLine } from "lucide-vue-next";
import { motion, useReducedMotion } from "motion-v";
import Card from "./Card.vue";
import { airportNames } from "../lib/airports";
import { apiRequest, percent, type DataSource } from "../lib/dashboard";

const props = defineProps<{ mode: DataSource; initialMonth: string; initialAirport: string }>();
defineEmits<{ useWarehouse: []; inspectValidation: [] }>();

interface RiskFactor {
  feature: string;
  name_cn: string;
  feature_value?: number | null;
  contribution_logit?: number;
  impact_pp: number;
}

interface PredictionResult {
  flight: { origin: string; dest: string; airline: string; flight_date: string; crs_dep_time: string };
  prediction: { prob_delay: number; risk_level: "low" | "medium" | "high"; threshold: number };
  explanation: { factors: RiskFactor[] };
  model_ver: string;
  engine: string;
  meta: { weather_source: string; rate_source_period: string | null; feature_profile?: "form" };
}

import { airlines } from "../lib/airlines";
const riskLabels = { low: "低风险", medium: "中风险", high: "高风险" };
const weatherLabels: Record<string, string> = {
  observation: "起飞前 1–3 小时的历史观测",
  climatology: "历史气候估计，缺测时取默认值",
  default: "缺少观测，采用默认值",
  missing: "缺少起飞前观测，气象特征按缺失处理",
};
const inputUnits: Record<string, string> = {
  distance_mi: "英里",
  vis_min_mi: "英里",
  wind_max_ms: "m/s",
  tmp_mean_c: "℃",
  precip_mm: "mm",
  crs_elapsed_min: "分钟",
  prev_arr_delay_min: "分钟",
  turnaround_min: "分钟",
  origin_traffic_prev_day: "班",
  dep_hour: "时",
  month: "月",
};
const binaryInputs = new Set(["is_weekend", "is_holiday", "chain_available", "risk_flag", "weather_available"]);
const weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"];
function formatModelInput({ feature, feature_value: value }: RiskFactor) {
  if (value == null) return "缺失";
  if (feature.includes("_delay_rate_")) return percent(value) + "%";
  if (feature === "day_of_week" && weekdays[value]) return weekdays[value];
  if (binaryInputs.has(feature)) return value === 1 ? "是" : "否";
  const display = value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  const unit = inputUnits[feature];
  return unit ? `${display} ${unit}` : display;
}
function formatImpact(factor: RiskFactor) {
  const effect = factor.contribution_logit || factor.impact_pp;
  if (effect === 0) return "影响接近零";
  const direction = effect > 0 ? "推高风险" : "拉低风险";
  const magnitude = Math.abs(factor.impact_pp);
  return `${direction} · 约 ${magnitude ? magnitude.toFixed(2) : "<0.01"} 个百分点`;
}
const origin = props.initialAirport || "ORD";
const plan = reactive({
  origin, dest: origin === "LAX" ? "ORD" : "LAX", airline: "AA",
  date: props.initialMonth ? `${props.initialMonth}-15` : "",
  time: "08:30",
});
const result = ref<PredictionResult | null>(null);
const busy = ref(false);
const error = ref("");
const reducedMotion = useReducedMotion();
let active: AbortController | undefined;

function invalidate() {
  active?.abort();
  active = undefined;
  result.value = null;
  error.value = "";
  busy.value = false;
}
watch(plan, invalidate, { flush: "sync" });
onBeforeUnmount(invalidate);

async function predict() {
  if (props.mode !== "api" || busy.value) return;
  invalidate();
  if (plan.origin === plan.dest) {
    error.value = "出发和到达机场相同，请重新选择到达机场。";
    return;
  }
  const request = new AbortController();
  active = request;
  busy.value = true;
  const timeout = window.setTimeout(() => request.abort("timeout"), 30000);
  try {
    const response = await apiRequest<PredictionResult>("predict", request.signal, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        origin: plan.origin, dest: plan.dest, airline: plan.airline,
        flight_date: plan.date, crs_dep_time: plan.time.replace(":", ""), top_k: 4,
      }),
    });
    if (request !== active) return;
    if (!response?.flight || !Number.isFinite(response.prediction?.prob_delay) ||
        !Array.isArray(response.explanation?.factors) || !response.meta) {
      throw new Error("预测服务返回的数据不完整，请重试。");
    }
    result.value = response;
  } catch (cause) {
    if (request !== active) return;
    error.value = request.signal.aborted ? "预测请求超时，请重试。"
      : cause instanceof TypeError ? "无法连接预测服务，请检查后端连接后重试。"
      : cause instanceof Error ? cause.message : "预测失败，请重试。";
  } finally {
    window.clearTimeout(timeout);
    if (request === active) {
      busy.value = false;
      active = undefined;
    }
  }
}
</script>

<template>
  <Card v-if="mode === 'demo'" title="单航班预测" :icon="BrainCircuit">
    <div class="workspace-content">
      <p>当前使用演示数据。切换到数仓后，可以提交航班计划并查看模型结果。</p>
      <button type="button" class="button primary" @click="$emit('useWarehouse')">
        切换到数仓数据 <ArrowRight :size="15" aria-hidden="true" />
      </button>
    </div>
  </Card>
  <div v-else class="prediction-layout">
    <Card title="航班计划" :icon="PlaneTakeoff">
      <form class="plan-form" @submit.prevent="predict">
        <label>出发机场
          <select v-model="plan.origin" required>
            <option v-for="(name, code) in airportNames" :key="code" :value="code">{{ code }} · {{ name }}</option>
          </select>
        </label>
        <label>到达机场
          <select v-model="plan.dest" required>
            <option v-for="(name, code) in airportNames" :key="code" :value="code">{{ code }} · {{ name }}</option>
          </select>
        </label>
        <label class="full-field">航空公司
          <select v-model="plan.airline" required>
            <option v-for="(name, code) in airlines" :key="code" :value="code">{{ code }} · {{ name }}</option>
          </select>
        </label>
        <label>起飞日期<input v-model="plan.date" type="date" required /></label>
        <label>计划起飞时间<input v-model="plan.time" type="time" required /></label>
        <p class="form-note full-field">日期和时间均按出发机场当地时间填写。</p>
        <p v-if="error" class="form-error full-field" role="alert">{{ error }}</p>
        <div class="full-field">
          <button class="button primary" type="submit" :disabled="busy">
            <ScanLine :size="16" aria-hidden="true" />
            {{ busy ? "正在计算…" : "运行预测" }}
          </button>
        </div>
      </form>
    </Card>
    <Card title="风险评估" :icon="BrainCircuit">
      <template #footer><button class="text-button" @click="$emit('inspectValidation')">查看模型验证 <ArrowRight :size="14" /></button></template>
      <template #actions><span v-if="result" class="tag">{{ result.engine === "lightgbm" ? "LightGBM" : result.engine === "baseline" ? "基线模型" : result.engine }}</span></template>
      <div class="risk-body">
        <p class="sr-only" role="status">{{ busy ? "正在计算延误风险" : result ? "计算完成，延误概率 " + percent(result.prediction.prob_delay) + "%" : "" }}</p>
        <motion.div v-if="result" class="risk-result" :initial="{ opacity: reducedMotion ? 1 : 0.65 }" :animate="{ opacity: 1 }" :transition="{ duration: reducedMotion ? 0 : 0.16 }">
          <div class="result-flight">
            <strong>{{ result.flight.origin }} → {{ result.flight.dest }}</strong>
            <span>{{ result.flight.airline }} · {{ result.flight.flight_date }} · {{ result.flight.crs_dep_time.slice(0, 2) }}:{{ result.flight.crs_dep_time.slice(2) }}</span>
          </div>
          <p class="risk-label">离港延误 ≥ 15 分钟的概率</p>
          <div class="probability">
            <strong data-testid="prediction-probability">{{ percent(result.prediction.prob_delay) }}<small>%</small></strong>
            <span class="risk-band" :class="{ elevated: result.prediction.risk_level !== 'low' }">{{ riskLabels[result.prediction.risk_level] }}</span>
          </div>
          <p class="form-note">模型判定延误的概率阈值为 {{ percent(result.prediction.threshold) }}%。</p>
          <h3>影响模型预测的因素</h3>
          <ul class="factor-list">
            <li v-for="factor in result.explanation.factors" :key="factor.feature">
              <div>
                <span>{{ factor.name_cn }}</span>
                <small>模型输入 {{ formatModelInput(factor) }}</small>
              </div>
              <b>{{ formatImpact(factor) }}</b>
            </li>
          </ul>
          <template v-if="result.meta.feature_profile === 'form'">
            <p class="form-note">表单模型使用日期、计划时刻和航段的大圆距离。</p>
            <p v-if="result.meta.weather_source !== 'unused'" class="form-note">气象输入 · {{ weatherLabels[result.meta.weather_source] }}</p>
          </template>
          <p v-else class="form-note">气象输入 · {{ weatherLabels[result.meta.weather_source] || result.meta.weather_source }}。前序飞机信息按缺失处理。</p>
          <p class="form-note">百分点表示模型对输入的近似响应。</p>
          <details class="model-details">
            <summary>模型与输入来源</summary>
            <dl>
              <dt>模型版本</dt><dd>{{ result.model_ver }}</dd>
              <template v-if="result.meta.feature_profile !== 'form'">
                <dt>近期延误率</dt><dd>{{ result.meta.rate_source_period ? "起飞日前七日的历史记录" : "缺少近期记录，采用回退值" }}</dd>
              </template>
            </dl>
          </details>
        </motion.div>
        <div v-else class="risk-pending">
          <strong>{{ busy ? "正在读取历史特征并计算风险…" : "等待提交航班计划" }}</strong>
          <p>运行预测后，这里会显示延误概率、风险等级和主要影响因素。</p>
        </div>
      </div>
    </Card>
  </div>
</template>

<style scoped>
.prediction-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); align-items: start; gap: var(--gap); }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.plan-form, .risk-body { padding: 0 22px 24px; }
.plan-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 14px; }
.plan-form label { display: grid; min-width: 0; gap: 7px; font-weight: 550; }
.plan-form select, .plan-form input { width: 100%; min-width: 0; min-height: 42px; font-weight: 400; }
.full-field { grid-column: 1 / -1; }
.form-note, .risk-pending p, .result-flight span, .model-details dt { color: var(--muted); font-size: 13px; line-height: 1.65; }
.form-error { color: #86451f; }
.risk-pending { border-top: 1px solid var(--line); padding: 22px 0 8px; }
.risk-pending strong { font-size: 17px; }
.risk-pending p { margin-top: 8px; }
.result-flight, .probability { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 12px; }
.result-flight { padding-bottom: 14px; border-bottom: 1px solid var(--line); }
.result-flight strong { font-size: 17px; }
.risk-label { margin-top: 16px; }
.probability strong { font-size: 40px; font-weight: 600; }
.probability small { font-size: 18px; }
.risk-band { color: var(--primary); background: var(--surface-soft); padding: 3px 8px; border-radius: 5px; font-size: 13px; }
.risk-band.elevated { background: #f7efdf; color: #805821; }
.risk-result h3 { margin: 20px 0 4px; font-size: 15px; }
.factor-list { list-style: none; margin: 0 0 8px; padding: 0; }
.factor-list li { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 4px 10px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.factor-list small { display: block; margin-top: 3px; color: var(--muted); }
.factor-list b { font-size: 13px; font-weight: 550; }
.model-details { margin-top: 14px; font-size: 13px; }
.model-details summary { cursor: pointer; color: var(--primary); }
.model-details dl { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 8px 14px; }
.model-details dd { margin: 0; overflow-wrap: anywhere; align-self: center; }
@media (max-width: 920px) { .prediction-layout { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 540px) {
  .plan-form, .risk-body { padding: 0 16px 18px; }
  .plan-form { grid-template-columns: minmax(0, 1fr); gap: 14px; }
}
</style>
