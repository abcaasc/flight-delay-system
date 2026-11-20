<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { PlaneTakeoff, ChevronLeft, ChevronRight, Download, X } from "lucide-vue-next";
import Card from "./Card.vue";
import { ApiError, downloadCsv, monthRange, number, type Coverage } from "../lib/dashboard";
import { airportNames } from "../lib/airports";
import { airlines } from "../lib/airlines";
import { useQuery } from "../lib/useQuery";

const props = defineProps<{ month: string; origin: string; initialDelayed: boolean; refresh: number }>();
let range: ReturnType<typeof monthRange> | null = null;
try { range = monthRange(props.month); } catch { /* 月份控件可能暂时为空 */ }
const filters = reactive({ start: range?.date_from ?? "", end: range?.date_to ?? "", dest: "", airline: "",
  status: props.initialDelayed ? "delayed" : "", sort: "dep_delay_min" });
const page = ref(1);
const size = 20;
interface Flight {
  flight_sk: string; flight_date: string; origin: string; dest: string; reporting_airline: string;
  flight_number: string; crs_dep_hhmm: string | number | null; dep_hhmm: string | number | null;
  cancelled: number; dep_delay_min: number | null; arr_delay_min: number | null;
}
interface FlightDetail extends Flight {
  arrival_delay_reasons: {
    status: "reported" | "not_reported" | "not_applicable" | "source_unavailable";
    items: { key: string; label: string; minutes: number }[];
  };
}
const selectedFlight = ref<Flight | null>(null);
const detailDialog = ref<HTMLDialogElement | null>(null);
const { data: detail, error: detailError, loading: detailLoading, reload: reloadDetail } =
  useQuery<FlightDetail>(() => {
    const flight = selectedFlight.value;
    if (!flight) return "";
    return `flights/${encodeURIComponent(flight.flight_sk)}?flight_date=${flight.flight_date}`;
  });
const reasonMessages = {
  not_reported: "BTS 未提供原因分钟。",
  not_applicable: "此航班不符合 BTS 到港延误原因的填报条件。",
  source_unavailable: "当前数仓没有可匹配的 BTS 原始记录。",
};
function openDetail(flight: Flight) {
  selectedFlight.value = flight;
  detailDialog.value?.showModal();
}

const datesValid = computed(() => range && filters.start >= range.date_from && filters.end <= range.date_to
  && filters.start <= filters.end && !!filters.start && !!filters.end);
watch(filters, () => { page.value = 1; }, { flush: "sync" });
const { data, error, loading, reload } = useQuery<{ items: Flight[]; total: number; coverage: Coverage }>(() => {
  if (!datesValid.value) return "";
  const params = new URLSearchParams({ date_from: filters.start, date_to: filters.end,
    page: String(page.value), page_size: String(size), sort_by: filters.sort,
    order: filters.sort === "dep_delay_min" ? "desc" : "asc" });
  if (props.origin) params.set("origin", props.origin);
  if (filters.dest) params.set("dest", filters.dest);
  if (filters.airline) params.set("airline", filters.airline);
  if (filters.status) params.set("cancelled", filters.status === "cancelled" ? "true" : "false");
  if (filters.status === "delayed") params.set("min_delay", "15");
  return "flights?" + params;
});
watch(() => props.refresh, reload);
const empty = computed(() => data.value?.total === 0 || (error.value instanceof ApiError && error.value.code === 522));
const unavailable = computed(() => error.value instanceof ApiError && error.value.code === 521);
const sampled = computed(() => !!data.value?.coverage?.sampled_months.length);
function hhmm(raw: Flight["dep_hhmm"]) {
  if (raw == null || raw === "") return "—";
  const s = String(raw).padStart(4, "0");
  return s.slice(0, 2) + ":" + s.slice(2);
}
const headings = ["日期", "航班", "出发", "到达", "计划起飞", "实际起飞", "离港延误（分）", "到港延误（分）", "状态"];
function cells(f: Flight) {
  return [f.flight_date, f.reporting_airline + f.flight_number, f.origin, f.dest,
    hhmm(f.crs_dep_hhmm), hhmm(f.dep_hhmm), f.dep_delay_min ?? "—", f.arr_delay_min ?? "—",
    f.cancelled === 1 ? "已取消" : f.dep_delay_min == null ? "缺少记录" : f.dep_delay_min >= 15 ? "延误" : "准点"];
}
function exportPage() {
  if (data.value?.items.length) downloadCsv([headings, ...data.value.items.map(cells)], `航班明细-${sampled.value ? "抽样-" : ""}${props.month}-第${page.value}页.csv`);
}
</script>

<template>
  <Card class="flight-search-card" title="航班明细" :icon="PlaneTakeoff" :subtitle="`${month} · ${origin || '全部出港机场'} · 起飞时间采用当地时间`">
    <template #actions><button class="button" :disabled="!data?.items.length" @click="exportPage"><Download :size="14" />导出本页</button></template>
    <div class="work-filters">
      <label>开始日期<input type="date" v-model="filters.start" :min="range?.date_from" :max="range?.date_to" /></label>
      <label>结束日期<input type="date" v-model="filters.end" :min="filters.start" :max="range?.date_to" /></label>
      <label>到达机场<select v-model="filters.dest"><option value="">全部机场</option><option v-for="(name,id) in airportNames" :key="id" :value="id">{{ id }} · {{ name }}</option></select></label>
      <label>承运航司<select v-model="filters.airline"><option value="">全部航司</option><option v-for="(name,id) in airlines" :key="id" :value="id">{{ id }} · {{ name }}</option></select></label>
      <label>航班状态<select v-model="filters.status"><option value="">全部状态</option><option value="operated">已执行</option><option value="delayed">离港延误 ≥ 15 分钟</option><option value="cancelled">已取消</option></select></label>
      <label>排序<select v-model="filters.sort"><option value="dep_delay_min">离港延误从长到短</option><option value="flight_date">航班日期从早到晚</option></select></label>
    </div>
    <p v-if="sampled" class="work-message" role="status">本月显示抽样航班明细，查询结果和架次仅代表样本。</p>
    <p v-if="!datesValid" class="work-message" role="alert">请在所选统计月份内填写有效的起止日期。</p>
    <p v-else-if="loading" class="work-message" role="status">正在查询航班…</p>
    <p v-else-if="unavailable" class="work-message" role="status">该月份暂无逐班明细，请切换月份。</p>
    <p v-else-if="empty" class="work-message" role="status">{{ sampled ? "样本中没有符合条件的航班。" : "当前条件下没有航班。" }}</p>
    <div v-else-if="error" class="work-message" role="alert">{{ error.message }} <button class="text-button" @click="reload">重试</button></div>
    <div v-else-if="data" class="table-scroll"><table>
      <thead><tr><th v-for="name in headings" :key="name">{{ name }}</th></tr></thead>
      <tbody>
        <tr v-for="f in data.items" :key="f.flight_sk">
          <td v-for="(value, i) in cells(f)" :key="i">
            <button v-if="i === 1" type="button" class="text-button flight-link" aria-haspopup="dialog"
              :aria-label="`查看 ${value} 航班详情`" @click="openDetail(f)">{{ value }}</button>
            <template v-else>{{ value }}</template>
          </td>
        </tr>
      </tbody>
    </table></div>
    <template #footer><div class="rank-footer">
      <span aria-live="polite">{{ data ? `共 ${number(data.total)} 班 · 第 ${page} 页` : empty ? "共 0 班" : "每页 20 班" }}</span>
      <div class="page-buttons"><button class="button" aria-label="上一页" :disabled="loading || page === 1" @click="page--"><ChevronLeft :size="16" /></button>
        <button class="button" aria-label="下一页" :disabled="!data || page * size >= data.total" @click="page++"><ChevronRight :size="16" /></button></div>
    </div></template>
  </Card>
  <dialog ref="detailDialog" class="flight-dialog" aria-labelledby="flight-detail-heading" @close="selectedFlight = null">
    <div class="detail-heading">
      <div>
        <h2 id="flight-detail-heading">{{ selectedFlight?.reporting_airline }}{{ selectedFlight?.flight_number }} 航班详情</h2>
        <p v-if="selectedFlight">{{ selectedFlight.flight_date }} · {{ selectedFlight.origin }} → {{ selectedFlight.dest }}</p>
      </div>
      <button type="button" class="button" aria-label="关闭航班详情" autofocus @click="detailDialog?.close()"><X :size="18" /></button>
    </div>
    <p v-if="detailLoading" role="status">正在读取航班详情…</p>
    <p v-else-if="detailError" role="alert">{{ detailError.message }} <button type="button" class="text-button" @click="reloadDetail">重试</button></p>
    <template v-else-if="detail">
      <dl class="flight-delays">
        <div><dt>离港延误</dt><dd>{{ detail.dep_delay_min == null ? "—" : `${number(detail.dep_delay_min)} 分钟` }}</dd></div>
        <div><dt>到港延误</dt><dd>{{ detail.arr_delay_min == null ? "—" : `${number(detail.arr_delay_min)} 分钟` }}</dd></div>
      </dl>
      <h3>BTS 上报的延误原因</h3>
      <dl v-if="detail.arrival_delay_reasons?.status === 'reported'" class="flight-reasons">
        <div v-for="item in detail.arrival_delay_reasons.items" :key="item.key">
          <dt>{{ item.label }}</dt><dd>{{ number(item.minutes) }} 分钟</dd>
        </div>
      </dl>
      <p v-else>{{ reasonMessages[detail.arrival_delay_reasons?.status ?? "not_reported"] }}</p>
      <p class="detail-note">范围为到港延误 ≥ 15 分钟的正常抵达航班。</p>
    </template>
  </dialog>
</template>

<style scoped>
.flight-link { padding: 0; text-decoration: underline; text-underline-offset: 3px; }
.flight-dialog { width: min(470px, calc(100% - 32px)); max-height: calc(100% - 32px); padding: 20px; border: 1px solid var(--line); border-radius: var(--radius-card); background: var(--surface); color: var(--ink); }
.flight-dialog::backdrop { background: rgb(20 60 50 / 45%); }
.detail-heading { display: flex; align-items: start; justify-content: space-between; gap: 10px; margin-bottom: 18px; }
.detail-heading p, .flight-delays dt, .detail-note { color: var(--muted); font-size: 13px; }
.detail-heading p { margin-top: 4px; }
.flight-delays { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; padding: 12px; background: var(--surface-soft); border-radius: var(--radius-control); }
.flight-dialog h3 { margin: 18px 0 8px; font-size: 15px; }
.flight-dialog dd { margin: 0; font-weight: 600; }
.flight-delays dd { margin-top: 5px; }
.flight-reasons { margin: 0; }
.flight-reasons div { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; padding: 8px 0; border-bottom: 1px solid var(--line); }
.detail-note { margin-top: 12px; }
</style>
