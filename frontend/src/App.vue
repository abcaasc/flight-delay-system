<script setup lang="ts">
import {
  ref,
  computed,
  watch,
  onBeforeUnmount,
  onMounted,
  defineAsyncComponent,
} from "vue";
import {
  Plane,
  PlaneTakeoff,
  LayoutDashboard,
  Network,
  ChartNoAxesCombined,
  Search,
  BrainCircuit,
  Database,
  Settings,
  Download,
  RefreshCw,
  ChevronRight,
  ArrowRight,
  SlidersHorizontal,
  CalendarDays,
  MapPin,
  CircleCheck,
  CircleX,
  Clock3,
  Info,
  AlertCircle,
  ListFilter,
  Maximize,
  Minimize,
  X,
  Radar,
  ChevronDown,
} from "lucide-vue-next";
import { motion, useReducedMotion } from "motion-v";
import Card from "./components/Card.vue";
const FlightSearch = defineAsyncComponent(() => import("./components/FlightSearch.vue"));
const DelayReasons = defineAsyncComponent(() => import("./components/DelayReasons.vue"));
const DataManagement = defineAsyncComponent(() => import("./components/DataManagement.vue"));
import { airportName, airportNames } from "./lib/airports";
const PredictionWorkspace = defineAsyncComponent(
  () => import("./components/PredictionWorkspace.vue"),
);
const NetworkMap = defineAsyncComponent(
  () => import("./components/NetworkMap.vue"),
);
const TrendChart = defineAsyncComponent(
  () => import("./components/TrendChart.vue"),
);
import {
  demoAirports,
  demoDashboard,
  fetchDashboard,
  apiRequest,
  type Coverage,
  downloadCsv,
  percent,
  number,
  type DashboardData,
  type DataSource,
  type MapMetric,
} from "./lib/dashboard";

const nav = [
  {
    name: "运行总览",
    icon: LayoutDashboard,
    description: "查看当前统计期的航班运行概况与重点机场。",
  },
  {
    name: "航线网络",
    icon: Network,
    description: "按月份与出港机场，观察航线连接和延误分布。",
  },
  {
    name: "多维分析",
    icon: ChartNoAxesCombined,
    description: "查看到港延误原因，对比每日运行波动与机场表现。",
  },
  {
    name: "航班查询",
    icon: Search,
    description: "按日期、航线和航司查询航班，核对计划起飞、实际起飞与延误时间。",
  },
  {
    name: "延误预测",
    icon: BrainCircuit,
    description: "输入一班航班的计划，估算离港延误 15 分钟及以上的概率。",
  },
  {
    name: "数据管理",
    icon: Database,
    description: "查看加工作业、失败记录、月度数据质量与模型验证结果。",
  },
];
// 默认统计月份：公网演示数据只带 2024 年，因此默认落到有数据的最近月份；
// 需要别的起始月份时用 VITE_DEFAULT_MONTH 覆盖。
const DEFAULT_MONTH = import.meta.env.VITE_DEFAULT_MONTH?.trim() || "2024-12";
const page = ref("运行总览"),
  airport = ref(""),
  month = ref(DEFAULT_MONTH),
  query = ref("");
const mode = ref<DataSource>("api"),
  selected = ref(""),
  metric = ref<MapMetric>("delay_rate");
const trendMetric = ref<"delay_rate" | "avg_delay_min">("delay_rate");
const data = ref<DashboardData | null>(null),
  error = ref(""),
  loading = ref(false),
  updated = ref("");
const lastReadyMonth = { api: "", demo: "" };
const fallbackMonth = ref("");
const fallbackMonthLabel = computed(() => {
  const [year, monthNumber] = fallbackMonth.value.split("-");
  return `${year} 年 ${Number(monthNumber)} 月`;
});
const airportOptions = ref<{ id: string; name: string }[]>(demoAirports),
  fullscreen = ref(false);
const reducedMotion = useReducedMotion();
let controller: AbortController | undefined,
  requestId = 0;
const currentPage = computed(() => nav.find((n) => n.name === page.value));
const currentIcon = computed(() => currentPage.value?.icon || Settings);
const isPrediction = computed(() => page.value === "延误预测");
const isManagement = computed(() => page.value === "数据管理");
const flightTab = ref<"明细" | "汇总">("明细");
const showFlights = computed(() => page.value === "航班查询" && mode.value === "api" && flightTab.value === "明细");
const showOverview = computed(() => !isManagement.value && !isPrediction.value && !showFlights.value);
const minOperated = ref<number | "">(0);
const delayedOnly = ref(false);
const flightRefresh = ref(0);
const allAirports = Object.entries(airportNames).map(([id, name]) => ({ id, name }));
const validationRequested = ref(false);
const scopedNodes = computed(() =>
  (data.value?.nodes || []).filter(
    (n) => !airport.value || n.id === airport.value,
  ),
);
const ranking = computed(() =>
  [...scopedNodes.value]
    .filter((n) => n.operated > 0 && n.operated >= Number(minOperated.value))
    .sort((a, b) => b.delayed / b.operated - a.delayed / a.operated || b.operated - a.operated || a.id.localeCompare(b.id)),
);
const filteredRows = computed(() =>
  scopedNodes.value.filter((n) =>
    `${n.id} ${airportName(n.id, n.name)} ${n.name} ${n.city || ""}`
      .toLowerCase()
      .includes(query.value.toLowerCase()),
  ),
);
const links = computed(() => data.value?.links || []);
const networkNodes = computed(() => {
  if (!airport.value) return data.value?.nodes || [];
  const ids = new Set([
    airport.value,
    ...links.value.flatMap((l) => [l.source, l.target]),
  ]);
  return (data.value?.nodes || []).filter((n) => ids.has(n.id));
});
const chosen = computed(() =>
  data.value?.nodes.find((n) => n.id === selected.value),
);
const topAirport = computed(() => ranking.value[0]);
const metrics = computed(() => {
  const k = data.value?.kpi;
  return [
    {
      label: "航班总量",
      value: number(k?.flights),
      unit: "架次",
      note: "当前范围内计划出港航班",
      icon: PlaneTakeoff,
    },
    {
      label: "航班准点率",
      value: percent(k?.ontime_rate),
      unit: "%",
      note: "已执行航班的准点占比",
      icon: CircleCheck,
    },
    {
      label: "平均离港延误",
      value: k?.avg_delay_min?.toFixed(1) ?? "—",
      unit: "分钟",
      note: "已执行航班平均值，含提前起飞",
      icon: Clock3,
    },
    {
      label: "航班取消率",
      value: percent(k?.cancel_rate, 2),
      unit: "%",
      note: "取消航班 / 计划出港航班",
      icon: CircleX,
    },
  ];
});
async function load() {
  const id = ++requestId;
  controller?.abort();
  controller = new AbortController();
  const activeController = controller;
  data.value = null;
  error.value = "";
  loading.value = true;
  updated.value = "";
  fallbackMonth.value = lastReadyMonth[mode.value];
  const timeout = setTimeout(() => activeController.abort("timeout"), 15000);
  try {
    const result =
      mode.value === "demo"
        ? demoDashboard(month.value, airport.value)
        : await fetchDashboard(
            month.value,
            airport.value,
            activeController.signal,
          );
    if (id !== requestId) return;
    data.value = result;
    lastReadyMonth[mode.value] = month.value;
    airportOptions.value = result.nodes
      .map((n) => ({ id: n.id, name: airportName(n.id, n.name) }))
      .sort((a, b) => a.id.localeCompare(b.id));
    if (!result.nodes.some((n) => n.id === selected.value)) selected.value = "";
    if (airport.value) selected.value = airport.value;
    updated.value = new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
  } catch (e) {
    if (id !== requestId) return;
    error.value = activeController.signal.aborted
      ? "请求超时，请检查后端服务并重试。"
      : e instanceof Error
        ? e.message
        : "读取失败，请重试";
    if (mode.value === "api" && !fallbackMonth.value && !activeController.signal.aborted) {
      apiRequest<{ dashboard: Coverage }>("system/coverage", activeController.signal)
        .then(({ dashboard }) => {
          if (id === requestId) fallbackMonth.value = dashboard.available_months.at(-1) ?? "";
        }).catch(() => {});
    }
  } finally {
    clearTimeout(timeout);
    if (id === requestId) loading.value = false;
  }
}
watch(mode, () => {
  airport.value = "";
  selected.value = "";
  query.value = "";
  airportOptions.value = mode.value === "demo" ? demoAirports : [];
});
watch([month, airport, mode, showOverview], () => {
  if (showOverview.value) { load(); return; }
  requestId++;
  controller?.abort();
  loading.value = false;
}, { immediate: true });
function syncFullscreen() {
  fullscreen.value = !!document.fullscreenElement;
}
onMounted(() => document.addEventListener("fullscreenchange", syncFullscreen));
onBeforeUnmount(() => {
  controller?.abort();
  document.removeEventListener("fullscreenchange", syncFullscreen);
});
function resetScope() {
  airport.value = "";
  query.value = "";
  selected.value = "";
}
function selectAirport(id: string) {
  selected.value = selected.value === id ? "" : id;
}
function goToAirport(id: string) {
  airport.value = id;
  selected.value = id;
  page.value = "航线网络";
}
function inspectDelayed(id: string) {
  airport.value = id;
  delayedOnly.value = true;
  flightTab.value = "明细";
  page.value = "航班查询";
}
watch(page, (next) => {
  if (next !== "航班查询") delayedOnly.value = false;
  if (next !== "数据管理") validationRequested.value = false;
});
function openValidation() {
  validationRequested.value = true;
  page.value = "数据管理";
}
function restoreMonth() {
  if (fallbackMonth.value && (month.value !== fallbackMonth.value || airport.value)) {
    month.value = fallbackMonth.value;
    airport.value = "";
  } else load();
}
function exportCsv() {
  if (!data.value) return;
  const rows: (string | number | null)[][] = [
    [
      "数据来源",
      "统计月份",
      "出港机场范围",
      "机场代码",
      "机场名称",
      "计划出港航班量",
      "延误率(%)",
      "平均延误(分钟)",
    ],
    ...filteredRows.value.map((n) => [
      mode.value === "api" ? "数仓接口" : "模拟数据",
      month.value,
      airport.value || "全部机场",
      n.id,
      airportName(n.id, n.name),
      n.flights,
      n.delay_rate == null ? null : Number(percent(n.delay_rate, 2)),
      n.avg_delay_min == null ? null : Number(n.avg_delay_min.toFixed(2)),
    ]),
  ];
  downloadCsv(
    rows,
    `机场运行分析-${mode.value === "api" ? "数仓" : "模拟"}-${month.value}-${airport.value || "全部机场"}.csv`,
  );
}
async function toggleFullscreen() {
  try {
    if (document.fullscreenElement) await document.exitFullscreen();
    else await document.documentElement.requestFullscreen();
    fullscreen.value = !!document.fullscreenElement;
  } catch {
    fullscreen.value = !fullscreen.value;
  }
}
</script>

<template>
  <a href="#main-content" class="skip-link">跳至分析内容</a>
  <div class="shell" :class="{ 'screen-mode': fullscreen }">
    <aside class="sidebar" aria-label="主导航">
      <a class="brand" href="#main-content" @click="page = '运行总览'"
        ><span class="brand-icon"><Plane :size="23" :stroke-width="1.7" /></span
        ><span class="brand-name">航迹<small>AIRTRACE</small></span></a
      >
      <div class="workspace">
        <span class="status-dot"></span>航空运行分析工作台
      </div>
      <p class="nav-label">分析空间</p>
      <nav>
        <button
          v-for="n in nav"
          :key="n.name"
          :class="{ active: page === n.name }"
          :aria-current="page === n.name ? 'page' : undefined"
          :aria-label="n.name"
          :title="n.name"
          @click="page = n.name"
        >
          <component
            :is="n.icon"
            :size="19"
            :stroke-width="1.7"
            aria-hidden="true"
          /><span>{{ n.name }}</span
          ><ChevronRight
            v-if="page === n.name"
            :size="14"
            class="nav-arrow"
            aria-hidden="true"
          />
        </button>
      </nav>
      <div class="sidebar-bottom">
        <div class="data-note">
          <span class="status-dot"></span>
          <div>历史数据研究环境<small>BTS · NOAA · OpenFlights</small></div>
        </div>
        <button
          class="settings-link"
          :class="{ active: page === '系统设置' }"
          :aria-current="page === '系统设置' ? 'page' : undefined"
          aria-label="系统设置"
          title="系统设置"
          @click="page = '系统设置'"
        >
          <Settings :size="19" :stroke-width="1.7" /><span>系统设置</span>
        </button>
        <div class="user">
          <span>研</span>
          <div>研究工作区<small>航空数据分析人员</small></div>
        </div>
      </div>
    </aside>
    <div class="main-area">
      <header class="topbar">
        <div class="breadcrumb">
          <span>分析空间</span><ChevronRight :size="13" /><component
            :is="currentIcon"
            :size="15"
            :stroke-width="1.7"
          /><strong>{{ page }}</strong>
        </div>
        <div class="header-right">
          <span class="domain"
            ><span class="status-dot"></span>美国国内航班</span
          ><label class="source-select"
            ><Database :size="14" /><select
              v-model="mode"
              aria-label="数据来源"
            >
              <option value="api">数仓数据</option>
              <option value="demo">交互演示</option>
            </select></label
          ><button
            class="icon-button"
            :aria-label="fullscreen ? '退出大屏' : '进入大屏'"
            :title="fullscreen ? '退出大屏' : '进入大屏'"
            @click="toggleFullscreen"
          >
            <component :is="fullscreen ? Minimize : Maximize" :size="17" />
          </button>
        </div>
      </header>
      <main
        id="main-content"
        class="content"
        :class="{
          'overview-screen':
            fullscreen && page === '运行总览' && data && !loading && !error,
        }"
        tabindex="-1"
      >
        <div class="heading">
          <div>
            <h1>{{ page }}</h1>
            <p>{{ currentPage?.description || "管理分析环境与数据来源。" }}</p>
          </div>
          <motion.button
            v-if="showOverview"
            class="button"
            :disabled="!data || loading"
            :while-hover="{ y: reducedMotion ? 0 : -1 }"
            :while-press="{ scale: reducedMotion ? 1 : 0.98 }"
            :transition="{ duration: 0.16 }"
            @click="exportCsv"
            ><Download :size="16" />导出分析</motion.button
          >
        </div>
        <div v-if="page === '航班查询' && mode === 'api'" class="segmented workspace-tabs" aria-label="查询内容">
          <button v-for="tab in (['明细','汇总'] as const)" :key="tab" :class="{ selected: flightTab === tab }"
            :aria-pressed="flightTab === tab" @click="flightTab = tab">{{ tab === '明细' ? '航班明细' : '机场汇总' }}</button>
        </div>
        <section v-if="!isPrediction && !isManagement" class="filters" aria-label="全局分析筛选">
          <div class="filter-group">
            <label
              ><span><CalendarDays :size="15" />统计月份</span
              ><input
                type="month"
                v-model="month"
                min="2015-01"
                max="2024-12"
                aria-label="统计月份"
            /></label>
            <div class="filter-divider"></div>
            <label
              ><span><MapPin :size="15" />出港机场</span
              ><select v-model="airport" aria-label="机场范围">
                <option value="">全部机场</option>
                <option v-for="c in (showFlights ? allAirports : airportOptions)" :key="c.id" :value="c.id">
                  {{ c.id }} · {{ c.name }}
                </option>
              </select></label
            >
          </div>
          <div class="filter-actions">
            <button
              v-if="airport"
              class="scope-chip"
              @click="resetScope"
              aria-label="清除机场筛选"
            >
              {{ airport }}<X :size="13" /></button
            ><span class="filter-hint"
              ><SlidersHorizontal :size="15" />全局联动</span
            ><button
              class="icon-button refresh-button"
              aria-label="刷新分析数据"
              :disabled="loading"
              @click="showFlights ? flightRefresh++ : load()"
            >
              <RefreshCw :size="17" :class="{ spinning: loading }" />
            </button>
          </div>
        </section>
        <div v-if="showOverview" class="notice" aria-live="polite">
          <span
            ><Info :size="14" />{{
              mode === "demo"
                ? "模拟数据 · 各模块由同一组模拟记录聚合，不代表实际运行情况。"
                : "真实历史数据 · 美国国内 BTS 航班，按所选月份和机场统计。"
            }}</span
          ><span class="threshold"
            >离港延误 ≥ 15 分钟
            <span v-if="updated">· 更新于 {{ updated }}</span></span
          >
        </div>
        <p v-if="showOverview && data?.missingMonths?.length" class="work-message" role="status">
          当前仅展示部分数据，缺少 {{ data.missingMonths.join("、") }} 的统计分区。
        </p>
        <div v-if="showOverview" class="metrics" :aria-busy="loading">
          <article v-for="m in metrics" :key="m.label" class="card metric-card">
            <div class="metric-label">
              {{ m.label
              }}<span class="metric-icon"
                ><component
                  :is="m.icon"
                  :size="18"
                  :stroke-width="1.6"
                  aria-hidden="true"
              /></span>
            </div>
            <div
              v-if="loading"
              class="metric-skeleton"
              aria-label="正在加载"
            ></div>
            <div v-else class="metric-value" :data-testid="m.label">
              <strong>{{ m.value }}</strong><small>{{ m.unit }}</small>
            </div>
            <p>{{ m.note }}</p>
          </article>
        </div>
        <FlightSearch v-if="showFlights" :key="month + airport" :month="month" :origin="airport"
          :initial-delayed="delayedOnly" :refresh="flightRefresh" />
        <DataManagement v-else-if="isManagement" :key="mode" :mode="mode" :initial-month="month" :initial-validation="validationRequested" @use-warehouse="mode = 'api'" />
        <PredictionWorkspace
          v-else-if="isPrediction"
          :key="mode"
          :mode="mode"
          :initial-month="month"
          :initial-airport="airport"
          @use-warehouse="mode = 'api'"
          @inspect-validation="openValidation"
        />
        <section v-else-if="error" class="card state-panel" role="alert">
          <span class="state-icon"><AlertCircle :size="25" /></span>
          <div>
            <h2>当前范围暂无可用分析数据</h2>
            <p>{{ error }}</p>
            <p>请确认所选月份已完成导入和统计加工。切换月份后，旧指标和图表已清空。</p>
            <div class="state-actions">
              <button class="button primary" @click="load">
                <RefreshCw :size="15" />重新读取</button
              ><button v-if="fallbackMonth && (fallbackMonth !== month || airport)" class="button" @click="restoreMonth">
                查看 {{ fallbackMonthLabel }}</button
              ><button class="text-button" @click="mode = 'demo'">
                体验模拟数据 <ArrowRight :size="14" />
              </button>
            </div>
          </div>
        </section>
        <div v-else-if="loading" class="loading-grid" role="status">
          <div class="card loading-panel">
            <Network :size="26" />
            <p>正在同步指标、航线网络与每日趋势…</p>
            <span>以当前月份与出港机场为统一统计范围</span>
          </div>
        </div>
        <template v-else-if="data">
          <motion.div
            v-if="['运行总览', '航线网络', '多维分析'].includes(page)"
            :initial="reducedMotion ? false : { opacity: 0.65 }"
            :animate="{ opacity: 1 }"
            :transition="{ duration: reducedMotion ? 0 : 0.18 }"
            class="dashboard-grid"
          >
            <Card
              v-if="page !== '多维分析'"
              class="network-panel"
              title="航线网络态势"
              :icon="Network"
              :subtitle="`${month} · ${airport || '全部出港机场'} · ${number(links.length)} 条航线`"
            >
              <template #actions
                ><div class="segmented" aria-label="地图展示指标">
                  <button
                    :class="{ selected: metric === 'delay_rate' }"
                    :aria-pressed="metric === 'delay_rate'"
                    @click="metric = 'delay_rate'"
                  >
                    延误率</button
                  ><button
                    :class="{ selected: metric === 'flights' }"
                    :aria-pressed="metric === 'flights'"
                    @click="metric = 'flights'"
                  >
                    航班量
                  </button>
                </div></template
              >
              <NetworkMap
                v-if="links.length"
                :nodes="networkNodes"
                :links="links"
                :selected="selected"
                :airport="airport"
                :metric="metric"
                @select="selectAirport"
              />
              <div v-else class="inline-empty">
                <Network :size="25" />
                <p>当前机场没有可绘制的出港航线</p>
                <button class="text-button" @click="resetScope">
                  查看全部机场
                </button>
              </div>
              <template #footer
                ><div v-if="chosen" class="airport-detail">
                  <div class="airport-identity">
                    <strong>{{ chosen.id }}</strong><span
                      :title="chosen.name"
                      >{{ airportName(chosen.id, chosen.name) }}</span
                    >
                  </div>
                  <div class="airport-stat">
                    延误率 <b>{{ percent(chosen.delay_rate) }}%</b>
                  </div>
                  <button class="text-button" @click="goToAirport(chosen.id)">
                    筛选此机场 <ArrowRight :size="14" />
                  </button>
                </div>
                <div v-else class="airport-detail">
                  <span class="selection-hint"
                    ><MapPin :size="15" />选择地图或排行中的机场查看详情</span
                  ><span class="tag"
                    >{{ number(networkNodes.length) }} 个关联节点</span
                  >
                </div></template
              >
            </Card>
            <DelayReasons v-if="page === '多维分析'" :month="month" :airport="airport" :demo="mode === 'demo'" />
            <Card
              class="ranking"
              title="机场延误排行"
              :icon="ListFilter"
              :subtitle="
                airport
                  ? `${airport} · 当前月出港口径`
                  : '按当前月出港延误率排序'
              "
            >
              <template #actions
                ><span class="tag">{{ month }}</span></template
              >
              <div class="rank-head">
                <label>最低执行架次 <input aria-label="最低执行架次" type="number" min="0" step="1" v-model.number="minOperated"
                  @change="minOperated = Math.max(0, Math.ceil(Number(minOperated) || 0))" /></label>
                <span>延误 / 执行 · 延误率</span>
              </div>
              <div v-if="!ranking.length" class="inline-empty">
                <p>当前样本条件下没有可排名的机场</p>
                <button class="text-button" @click="minOperated = 0; resetScope()">
                  重置排行筛选
                </button>
              </div>
              <button
                v-for="(c, i) in ranking.slice(0, 6)"
                :key="c.id"
                class="rank-row"
                :class="{ 'is-selected': selected === c.id }"
                :aria-label="`高亮 ${c.id} ${airportName(c.id, c.name)}，${c.operated} 架已执行，${c.delayed} 架延误，延误率 ${percent(c.delay_rate)}%`"
                :aria-pressed="selected === c.id"
                @click="selectAirport(c.id)"
              >
                <span class="rank-num">{{
                  String(i + 1).padStart(2, "0")
                }}</span>
                <div class="rank-info">
                  <strong
                    >{{ c.id
                    }}<span :title="airportName(c.id, c.name)">{{
                      airportName(c.id, c.name)
                    }}</span></strong
                  >
                  <div class="bar">
                    <span
                      :style="{ width: percent(c.delay_rate) + '%' }"
                      :class="{ amber: (c.delay_rate ?? 0) >= 0.3 }"
                    ></span>
                  </div>
                </div>
                <div class="rank-values"><b>{{ percent(c.delay_rate) }}<small>%</small></b>
                  <small>{{ number(c.delayed) }} / {{ number(c.operated) }}</small></div>
              </button>
              <template #footer
                ><div class="rank-footer"><span class="muted">{{ number(ranking.length) }} 个机场符合条件</span>
                  <button v-if="mode === 'api' && selected && ranking.some(n => n.id === selected)"
                    class="text-button" @click="inspectDelayed(selected)">查看 {{ selected }} 延误航班 <ArrowRight :size="14" /></button>
                  <span v-else class="muted">点击机场查看详情</span>
                </div></template
              >
            </Card>
            <Card
              v-if="page !== '航线网络'"
              class="trend-panel"
              title="运行趋势"
              :icon="ChartNoAxesCombined"
              :subtitle="`${month} · ${airport || '全部机场'} · ${data.trend.length} 个观测日`"
              ><template #actions
                ><label class="compact-select"
                  ><select v-model="trendMetric" aria-label="趋势分析指标">
                    <option value="delay_rate">离港延误率</option>
                    <option value="avg_delay_min">平均延误时长</option></select
                  ><ChevronDown :size="13" /></label></template
              ><TrendChart
                :points="data.trend"
                :metric="trendMetric"
                :reduced="!!reducedMotion"
            /></Card>
            <Card
              v-if="page !== '航线网络'"
              class="insight"
              title="运行观察"
              :icon="Radar"
              subtitle="基于当前筛选范围的统计摘要"
              ><template v-if="topAirport"
                ><div class="insight-body">
                  <span class="tag"
                    >{{ mode === "demo" ? "模拟统计" : "数仓统计" }} ·
                    {{ month }}</span
                  >
                  <h3>
                    {{ topAirport.id }} 的离港延误率为
                    {{ percent(topAirport.delay_rate) }}%
                  </h3>
                  <p>
                    在符合排行条件的机场中位列第一，共
                    {{ number(topAirport.flights) }}
                    架次计划出港航班，平均离港延误
                    {{ topAirport.avg_delay_min?.toFixed(1) ?? "—" }} 分钟。
                  </p>
                  <p class="insight-footnote">
                    历史统计用于识别运行波动，不等同于单航班风险预测。
                  </p>
                  <button
                    class="text-button"
                    @click="goToAirport(topAirport.id)"
                  >
                    查看该机场航线 <ArrowRight :size="15" />
                  </button></div
              ></template>
              <div v-else class="inline-empty">
                没有可供分析的机场数据
              </div></Card
            >
            <Card
              v-if="page === '航线网络'"
              class="routes-panel"
              title="出港航线明细"
              :icon="PlaneTakeoff"
              subtitle="与网络图使用相同的月份和机场范围"
              ><div class="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>航线</th>
                      <th>计划航班量</th>
                      <th>延误率</th>
                      <th>平均延误</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="l in [...links]
                        .sort((a, b) => b.flights - a.flights)
                        .slice(0, 12)"
                      :key="l.source + l.target"
                    >
                      <td>
                        <span class="route-code"
                          >{{ l.source }} <ArrowRight :size="12" />
                          {{ l.target }}</span
                        >
                      </td>
                      <td>{{ number(l.flights) }}</td>
                      <td>{{ percent(l.delay_rate) }}%</td>
                      <td>{{ l.avg_delay_min?.toFixed(1) ?? "—" }} 分钟</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <template #footer
                ><span class="muted"
                  >展示流量最高的 12 条航线，网络最多读取前 1,000 条航线。</span
                ></template
              ></Card
            >
          </motion.div>
          <Card
            v-if="page === '航班查询'"
            title="机场出港统计查询"
            :icon="Search"
            :subtitle="`${month} · 当前统计范围，不是逐航班明细`"
            ><template #actions
              ><label class="search"
                ><Search :size="15" /><input
                  v-model="query"
                  placeholder="搜索机场代码或名称"
                  aria-label="搜索机场" /></label
            ></template>
            <div class="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>机场代码</th>
                    <th>机场名称</th>
                    <th>计划出港航班</th>
                    <th>延误率</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="n in filteredRows" :key="n.id">
                    <td>
                      <strong>{{ n.id }}</strong>
                    </td>
                    <td :title="n.name">{{ airportName(n.id, n.name) }}</td>
                    <td>{{ number(n.flights) }}</td>
                    <td>{{ percent(n.delay_rate) }}%</td>
                    <td>
                      <button class="text-button" @click="goToAirport(n.id)">
                        查看航线<ArrowRight :size="14" />
                      </button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-if="!filteredRows.length" class="inline-empty">
              <p>没有匹配的机场</p>
              <button class="text-button" @click="resetScope">
                清除筛选和搜索
              </button>
            </div></Card
          >
          <Card
            v-if="page === '系统设置'"
            :title="page + '工作区'"
            :icon="currentIcon"
            ><div class="workspace-content">
              <dl class="environment">
                  <div>
                    <dt>当前数据来源</dt>
                    <dd>
                      {{ mode === "api" ? "Flask 数仓接口" : "同源模拟记录" }}
                    </dd>
                  </div>
                  <div>
                    <dt>统计范围</dt>
                    <dd>{{ month }} · {{ airport || "全部出港机场" }}</dd>
                  </div>
                  <div>
                    <dt>数据源</dt>
                    <dd>BTS / NOAA / OpenFlights</dd>
                  </div>
                  <div>
                    <dt>连接方式</dt>
                    <dd>/api → Flask 服务</dd>
                  </div>
                </dl>
                <label class="settings-mode"
                  >数据来源<select v-model="mode">
                    <option value="api">数仓数据</option>
                    <option value="demo">交互演示</option>
                  </select></label
                >
            </div></Card
          >
        </template>
        <footer>
          <span>航迹 AIRTRACE · 航班运行分析</span
          ><span
            >数据：BTS / NOAA / OpenFlights（ODbL） · 底图：U.S. Census
            Bureau</span
          >
        </footer>
      </main>
    </div>
  </div>
</template>
