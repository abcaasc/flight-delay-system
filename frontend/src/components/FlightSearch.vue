<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { PlaneTakeoff, ChevronLeft, ChevronRight, Download } from "lucide-vue-next";
import Card from "./Card.vue";
import { ApiError, downloadCsv, monthRange, number } from "../lib/dashboard";
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
const datesValid = computed(() => range && filters.start >= range.date_from && filters.end <= range.date_to
  && filters.start <= filters.end && !!filters.start && !!filters.end);
watch(filters, () => { page.value = 1; }, { flush: "sync" });
const { data, error, loading, reload } = useQuery<{ items: Flight[]; total: number }>(() => {
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
  if (data.value?.items.length) downloadCsv([headings, ...data.value.items.map(cells)], `航班明细-${props.month}-第${page.value}页.csv`);
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
    <p v-if="!datesValid" class="work-message" role="alert">请在所选统计月份内填写有效的起止日期。</p>
    <p v-else-if="loading" class="work-message" role="status">正在查询航班…</p>
    <p v-else-if="empty" class="work-message" role="status">当前条件下没有航班。</p>
    <div v-else-if="error" class="work-message" role="alert">{{ error.message }} <button class="text-button" @click="reload">重试</button></div>
    <div v-else-if="data" class="table-scroll"><table>
      <thead><tr><th v-for="name in headings" :key="name">{{ name }}</th></tr></thead>
      <tbody><tr v-for="f in data.items" :key="f.flight_sk"><td v-for="(v,i) in cells(f)" :key="i">{{ v }}</td></tr></tbody>
    </table></div>
    <template #footer><div class="rank-footer">
      <span aria-live="polite">{{ data ? `共 ${number(data.total)} 班 · 第 ${page} 页` : empty ? "共 0 班" : "每页 20 班" }}</span>
      <div class="page-buttons"><button class="button" aria-label="上一页" :disabled="loading || page === 1" @click="page--"><ChevronLeft :size="16" /></button>
        <button class="button" aria-label="下一页" :disabled="!data || page * size >= data.total" @click="page++"><ChevronRight :size="16" /></button></div>
    </div></template>
  </Card>
</template>
