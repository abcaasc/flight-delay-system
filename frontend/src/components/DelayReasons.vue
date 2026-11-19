<script setup lang="ts">
import { computed } from "vue";
import { ListTree } from "lucide-vue-next";
import Card from "./Card.vue";
import { monthRange, number, percent } from "../lib/dashboard";
import { useQuery } from "../lib/useQuery";

const props = defineProps<{ month: string; airport: string; demo: boolean }>();
const { data, error, loading, reload } = useQuery<{
  arrival_delayed_flights: number;
  reported_flights: number;
  total_minutes: number;
  items: { key: string; label: string; minutes: number; flights: number; share: number | null }[];
}>(() => {
  if (props.demo) return "";
  let range;
  try { range = monthRange(props.month); } catch { return ""; }
  const params = new URLSearchParams(range);
  if (props.airport) params.set("airports", props.airport);
  return "overview/reasons?" + params;
});
const items = computed(() => [...(data.value?.items ?? [])].sort((a, b) => b.minutes - a.minutes));
</script>

<template>
  <Card title="到港延误原因" :icon="ListTree" :subtitle="`${month} · ${airport || '全部出港机场'} · BTS 到港延误 ≥ 15 分钟`">
    <p v-if="demo" class="work-message">切换到数仓数据后，可查看 BTS 上报的延误原因。</p>
    <p v-else-if="loading" class="work-message" role="status">正在汇总延误原因…</p>
    <div v-else-if="error" class="work-message" role="alert">
      {{ error.message }} <button type="button" class="text-button" @click="reload">重试</button>
    </div>
    <template v-else-if="data">
      <div class="reason-chart">
        <p class="coverage">有原因记录 <strong>{{ number(data.reported_flights) }} / {{ number(data.arrival_delayed_flights) }} 班</strong></p>
        <p v-if="!data.arrival_delayed_flights" class="coverage">当前筛选范围内没有到港延误达 15 分钟的航班。</p>
        <p v-else-if="!data.reported_flights" class="coverage">这些航班暂无 BTS 原因记录。</p>
        <ul v-else aria-label="各类原因的分钟数与占比">
          <li v-for="item in items" :key="item.key">
            <div class="reason-row">
              <span>{{ item.label }}</span>
              <span class="reason-value">{{ number(item.minutes) }} 分钟 · <strong>{{ percent(item.share) }}%</strong></span>
            </div>
            <div class="reason-track" aria-hidden="true"><span :style="{ width: `${(item.share ?? 0) * 100}%` }" /></div>
          </li>
        </ul>
      </div>
    </template>
    <template v-if="data?.reported_flights" #footer>
      共 {{ number(data.total_minutes) }} 分钟。占比按已上报的原因分钟计算，同一航班可有多项原因。
    </template>
  </Card>
</template>

<style scoped>
.reason-chart { padding: 0 20px 20px; }
.coverage { color: var(--muted); font-size: 13px; }
.coverage + .coverage { margin-top: 8px; }
.coverage strong { color: var(--ink); margin-left: 4px; }
ul { display: grid; gap: 12px; margin: 14px 0 0; padding: 0; list-style: none; }
.reason-row { display: flex; justify-content: space-between; flex-wrap: wrap; gap: 3px 10px; margin-bottom: 5px; font-size: 13px; }
.reason-value { color: var(--muted); }
.reason-value strong { color: var(--ink); }
.reason-track { height: 6px; background: var(--surface-soft); border-radius: 3px; overflow: hidden; }
.reason-track span { display: block; height: 100%; border-radius: inherit; background: var(--primary); }
</style>
