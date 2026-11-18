<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch, nextTick } from "vue";
import { init, use, type ECharts } from "echarts/core";
import { LineChart } from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { TrendPoint } from "../lib/dashboard";
use([
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  CanvasRenderer,
]);
const props = defineProps<{
  points: TrendPoint[];
  metric: "delay_rate" | "avg_delay_min";
  reduced: boolean;
}>();
const el = ref<HTMLElement>();
let chart: ECharts | undefined, observer: ResizeObserver | undefined;
function render() {
  if (!el.value) return;
  if (!chart) {
    chart = init(el.value);
    observer = new ResizeObserver(() => chart?.resize());
    observer.observe(el.value);
  }
  const isRate = props.metric === "delay_rate";
  chart.setOption(
    {
      animation: !props.reduced,
      animationDuration: 180,
      animationDurationUpdate: 180,
      color: ["#317c66"],
      textStyle: {
        fontFamily: '"Segoe UI", "Microsoft YaHei", sans-serif',
        color: "#61766b",
        fontSize: 11,
      },
      tooltip: {
        trigger: "axis",
        valueFormatter: (v: unknown) =>
          v == null
            ? "无数据"
            : Number(v).toFixed(1) + (isRate ? "%" : " 分钟"),
      },
      grid: { left: 48, right: 24, top: 24, bottom: 32 },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: props.points.map(
          (p) => `${p.bucket.slice(5, 7)}/${p.bucket.slice(8)}`,
        ),
        axisLine: { lineStyle: { color: "#dfe7e0" } },
        axisTick: { show: false },
        axisLabel: { hideOverlap: true },
      },
      yAxis: {
        type: "value",
        min: isRate ? 0 : undefined,
        axisLabel: { formatter: isRate ? "{value}%" : "{value}" },
        splitLine: { lineStyle: { color: "#edf1ec", type: "dashed" } },
      },
      series: [
        {
          type: "line",
          name: isRate ? "延误率" : "平均延误",
          data: props.points.map((p) =>
            p[props.metric] == null
              ? null
              : Number((p[props.metric]! * (isRate ? 100 : 1)).toFixed(2)),
          ),
          smooth: 0.2,
          symbol: "circle",
          symbolSize: 4,
          showSymbol: false,
          lineStyle: { width: 2 },
          areaStyle: { color: "#317c66", opacity: 0.05 },
        },
      ],
    },
    true,
  );
}
watch(
  () => [props.points, props.metric, props.reduced],
  async () => {
    await nextTick();
    render();
  },
);
onMounted(render);
onBeforeUnmount(() => {
  observer?.disconnect();
  chart?.dispose();
});
</script>
<template>
  <div
    ref="el"
    class="trend-chart"
    role="img"
    :aria-label="`${metric === 'delay_rate' ? '每日延误率' : '每日平均延误'}趋势图，共 ${points.length} 个观测日`"
  ></div>
  <details class="chart-data">
    <summary>查看每日数据</summary>
    <div class="table-scroll">
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>航班量</th>
            <th>延误率</th>
            <th>平均延误</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in points" :key="p.bucket">
            <td>{{ p.bucket }}</td>
            <td>{{ p.flights }}</td>
            <td>
              {{
                p.delay_rate == null
                  ? "—"
                  : (p.delay_rate * 100).toFixed(1) + "%"
              }}
            </td>
            <td>{{ p.avg_delay_min?.toFixed(1) ?? "—" }} 分钟</td>
          </tr>
        </tbody>
      </table>
    </div>
  </details>
</template>
