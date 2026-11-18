<script setup lang="ts">
import { computed } from "vue";
import { geoAlbersUsa, geoPath } from "d3-geo";
import { feature } from "topojson-client";
import type { Topology, GeometryCollection } from "topojson-specification";
import usStates from "../data/us-states.json";
import { airportName } from "../lib/airports";
import {
  percent,
  number,
  type AirportNode,
  type RouteLink,
  type MapMetric,
} from "../lib/dashboard";
const props = defineProps<{
  nodes: AirportNode[];
  links: RouteLink[];
  selected: string;
  airport: string;
  metric: MapMetric;
}>();
const emit = defineEmits<{ select: [id: string] }>();
const topology = usStates as unknown as Topology<{
  states: GeometryCollection;
}>;
const states = feature(topology, topology.objects.states);
const continental = {
  ...states,
  features: states.features.filter(
    (f) => !["02", "15", "72"].includes(String(f.id).padStart(2, "0")),
  ),
};
const projection = geoAlbersUsa().fitExtent(
  [
    [30, 20],
    [890, 460],
  ],
  continental,
);
const path = geoPath(projection);
const statePaths = continental.features.map((s) => ({
  id: s.id,
  d: path(s) || "",
}));
const points = computed(() =>
  props.nodes.flatMap((n) => {
    const xy =
      n.lon != null && n.lat != null ? projection([n.lon, n.lat]) : null;
    return xy ? [{ ...n, x: xy[0], y: xy[1] }] : [];
  }),
);
const nodeMap = computed(() => new Map(points.value.map((n) => [n.id, n])));
const routes = computed(() =>
  props.links
    .filter((l) => nodeMap.value.has(l.source) && nodeMap.value.has(l.target))
    .sort((a, b) => b.flights - a.flights)
    .slice(0, 160),
);
const maxFlights = computed(() =>
  Math.max(1, ...props.links.map((l) => l.flights)),
);
const labelThreshold = computed(
  () => Math.max(1, ...props.nodes.map((n) => n.flights)) * 0.15,
);
function routePath(l: RouteLink) {
  const a = nodeMap.value.get(l.source)!,
    b = nodeMap.value.get(l.target)!;
  const dx = b.x - a.x,
    dy = b.y - a.y;
  return `M${a.x},${a.y} Q${(a.x + b.x) / 2 + dy * 0.15},${(a.y + b.y) / 2 - dx * 0.15} ${b.x},${b.y}`;
}
function highlighted(l: RouteLink) {
  return l.source === props.selected || l.target === props.selected;
}
</script>
<template>
  <div class="network-map">
    <svg
      viewBox="0 0 920 485"
      role="group"
      aria-label="美国国内航线网络，机场节点支持键盘选择"
    >
      <g aria-hidden="true">
        <path
          v-for="s in statePaths"
          :key="String(s.id)"
          :d="s.d"
          class="state-shape"
        />
      </g>
      <g aria-hidden="true">
        <path
          v-for="l in routes"
          :key="l.source + '-' + l.target"
          :d="routePath(l)"
          fill="none"
          :stroke="
            metric === 'delay_rate' && (l.delay_rate ?? 0) >= 0.3
              ? '#bd9662'
              : '#6f9f8b'
          "
          :opacity="selected ? (highlighted(l) ? 0.65 : 0.08) : 0.28"
          :stroke-width="
            metric === 'flights'
              ? 1 + (2 * l.flights) / maxFlights
              : highlighted(l)
                ? 1.6
                : 0.85
          "
        >
          <title>
            {{ l.source }} → {{ l.target }} · {{ number(l.flights) }} 架次 ·
            延误率 {{ percent(l.delay_rate) }}%
          </title>
        </path>
      </g>
      <g
        v-for="n in points"
        :key="n.id"
        class="map-node"
        role="button"
        tabindex="0"
        :aria-label="`查看 ${n.id} ${airportName(n.id, n.name)}，延误率 ${percent(n.delay_rate)}%`"
        :aria-pressed="selected === n.id"
        @click="emit('select', n.id)"
        @keydown.enter="emit('select', n.id)"
        @keydown.space.prevent="emit('select', n.id)"
      >
        <title>
          {{ n.id }} · {{ airportName(n.id, n.name) }}&#10;{{ number(n.flights) }} 架次 · 延误率
          {{ percent(n.delay_rate) }}%
        </title>
        <circle :cx="n.x" :cy="n.y" r="12" fill="transparent" />
        <circle
          v-if="selected === n.id"
          :cx="n.x"
          :cy="n.y"
          r="11"
          fill="#e2ede5"
          stroke="#97b4a0"
        />
        <circle
          :cx="n.x"
          :cy="n.y"
          :r="selected === n.id ? 5 : 3.5"
          :fill="selected === n.id ? '#236d55' : '#fff'"
          :stroke="(n.delay_rate ?? 0) >= 0.3 ? '#b88f58' : '#56937b'"
          stroke-width="1.6"
        />
        <text
          v-if="
            nodes.length <= 16 ||
            n.id === selected ||
            n.id === airport ||
            n.flights >= labelThreshold
          "
          :x="n.x + 10"
          :y="n.y + 4"
        >
          {{ n.id }}
        </text>
      </g>
    </svg>
    <div class="map-legend">
      <span
        ><i></i
        >{{ metric === "delay_rate" ? "延误率 < 30%" : "线宽表示航班量" }}</span
      ><span v-if="metric === 'delay_rate'"
        ><i class="amber"></i>延误率 ≥ 30%</span
      ><span class="map-geography">美国本土 · 等积投影</span>
    </div>
  </div>
  <p class="map-note">
    {{
      airport
        ? `展示 ${airport} 的出港航线，目的机场作为关联节点。`
        : "显示流量最高的 160 条本土航线；点击机场高亮关联航线。"
    }}阿拉斯加、夏威夷等非本土机场计入统计，不绘于本图。
  </p>
</template>

