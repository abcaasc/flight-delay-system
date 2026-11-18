<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { Activity, ClipboardList, Database, LockKeyhole, RefreshCw } from "lucide-vue-next";
import Card from "./Card.vue";
import { ApiError, apiRequest, number, percent, type DataSource } from "../lib/dashboard";
import { session, type LoginSession } from "../lib/session";
import { useQuery } from "../lib/useQuery";

const props = defineProps<{ mode: DataSource; initialMonth: string; initialValidation: boolean }>();
defineEmits<{ useWarehouse: [] }>();
const tab = ref(props.initialValidation ? "模型验证" : "作业记录");
const month = ref(props.initialMonth || "2015-01");
const jobScope = ref("all");
const jobStatus = ref("");
const username = ref(""), password = ref(""), authError = ref(""), signingIn = ref(false);
const controller = new AbortController();
onBeforeUnmount(() => controller.abort());
async function login() {
  if (signingIn.value) return;
  signingIn.value = true;
  authError.value = "";
  try {
    const info = await apiRequest<LoginSession>("auth/login", AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]), {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username.value.trim(), password: password.value }),
    });
    if (controller.signal.aborted) return;
    if (info.user.role === "guest") authError.value = "此账号为访客，请使用运行监控员或更高权限的应用账号。";
    else session.value = info;
  } catch (e) {
    authError.value = e instanceof ApiError ? e.message : "登录服务无响应，请重试。";
  } finally { password.value = ""; signingIn.value = false; }
}
const statusLabels: Record<string,string> = { queued: "排队中", running: "运行中", success: "成功",
  failed: "失败", blocked: "受阻", cancelled: "已取消", skipped: "已跳过" };
interface Job { job_id: string; job_name: string; period: string; started_at: string; status: string; err_msg: string; err_code: string; progress: number }
interface Failure { id: number; dataset: string; period: string; file_name: string; err_msg: string; err_code: string; attempts: number }
interface Quality { rows: { ods: number; dwd: number; kept_rate: number | null }; drop_reasons: Record<string,number>; flag_reasons: Record<string,number>; audits: {id:number; file_name:string; status:string; drop_rows:number; err_code:string}[] }
interface Metrics { support: number; auc: number | null; recall: number; precision: number; f1:number; threshold: number }
interface Pair { full: Metrics; no_chain: Metrics }
interface Report { model_ver: string; current: boolean; generated_at: string; holdout: Pair & {test_range: string[]};
  folds: (Pair & {train: string[]; validation:string; test:string})[] }
const allowed = computed(() => props.mode === "api" && !!session.value);
const validMonth = computed(() => /^(201[5-9]|202[0-4])-(0[1-9]|1[0-2])$/.test(month.value));
const token = () => session.value?.token || "";
const {data: jobs, error: jobError, loading: jobsBusy, reload: loadJobs} = useQuery<{items:Job[]}>(() => {
  if (!allowed.value || tab.value !== "作业记录" || (jobScope.value === "month" && !validMonth.value)) return "";
  const p = new URLSearchParams({limit:"100"});
  if (jobScope.value === "month") p.set("period",month.value);
  if (jobStatus.value) p.set("status",jobStatus.value);
  return "ingest/jobs?" + p;
}, token);
const {data: quality, error: qualityError, loading: qualityBusy, reload: loadQuality} = useQuery<Quality>(() =>
  allowed.value && tab.value === "数据质量" && validMonth.value ? "quality/report?period=" + month.value : "", token);
const {data: failures, error: failureError, loading: failuresBusy, reload: loadFailures} = useQuery<{items:Failure[]}>(() =>
  allowed.value && tab.value === "失败文件" && session.value?.user.role === "admin" ? "ingest/failed?limit=100" : "", token);
const {data: report, error: reportError, loading: reportBusy, reload: loadReport} = useQuery<Report>(() =>
  allowed.value && tab.value === "模型验证" ? "model/backtest" : "", token);
const busy = computed(() => jobsBusy.value || qualityBusy.value || failuresBusy.value || reportBusy.value);
const error = computed(() => jobError.value || qualityError.value || failureError.value || reportError.value);
watch(error, e => {
  if (e instanceof ApiError && e.code === 401) { session.value = null; authError.value = "会话已过期，请重新登录。"; }
});
const tabs = computed(() => ["作业记录","数据质量", ...(session.value?.user.role === "admin" ? ["失败文件"] : []),"模型验证"]);
function logout() { session.value = null; authError.value = ""; tab.value = "作业记录"; }
function refresh() {
  if (tab.value === "作业记录") loadJobs();
  else if (tab.value === "数据质量") loadQuality();
  else if (tab.value === "失败文件") loadFailures();
  else loadReport();
}
const monthNeeded = computed(() => tab.value === "数据质量" || (tab.value === "作业记录" && jobScope.value === "month"));
const metric = (v:number | null) => v == null ? "—" : v.toFixed(3);
</script>

<template>
  <Card v-if="mode === 'demo'" title="数据管理" :icon="Database"><div class="workspace-content">
    <p>作业与质量报告保存在数仓环境。</p><button class="button primary" @click="$emit('useWarehouse')">切换到数仓数据</button>
  </div></Card>
  <Card v-else-if="!session" title="登录数据管理" :icon="LockKeyhole" subtitle="使用系统应用账号查看运行记录。管理员还可查看失败文件。">
    <form class="work-filters login-fields" @submit.prevent="login">
      <label>应用账号<input v-model="username" autocomplete="username" required /></label>
      <label>应用密码<input v-model="password" type="password" autocomplete="current-password" required /></label>
      <button class="button primary" :disabled="signingIn">{{ signingIn ? "正在登录…" : "登录" }}</button>
    </form><p v-if="authError" class="work-message" role="alert">{{ authError }}</p>
  </Card>
  <template v-else>
    <div class="work-toolbar"><div class="segmented workspace-tabs">
      <button v-for="t in tabs" :key="t" :class="{selected:tab === t}" :aria-pressed="tab === t" @click="tab=t">{{ t }}</button>
    </div><span>{{ session.user.username }} · {{ session.role_name }}　<button class="text-button" @click="logout">退出登录</button></span></div>
    <Card :title="tab" :icon="tab === '模型验证' ? Activity : ClipboardList">
      <template #actions><button class="button" :disabled="busy" @click="refresh"><RefreshCw :size="14" />刷新</button></template>
      <div v-if="tab === '作业记录' || monthNeeded" class="work-filters">
        <template v-if="tab === '作业记录'">
          <label>作业范围<select v-model="jobScope"><option value="all">全部月份</option><option value="month">指定月份</option></select></label>
          <label>作业状态<select v-model="jobStatus"><option value="">全部状态</option><option v-for="(name,code) in statusLabels" :key="code" :value="code">{{ name }}</option></select></label>
        </template>
        <label v-if="monthNeeded">报告月份<input type="month" min="2015-01" max="2024-12" v-model="month" /></label>
      </div>
      <p v-if="monthNeeded && !validMonth" class="work-message" role="alert">请选择 2015—2024 年之间的月份。</p>
      <p v-else-if="busy" class="work-message" role="status">正在读取{{ tab }}…</p>
      <p v-else-if="error" class="work-message" role="alert">{{ error.message }} <button class="text-button" @click="refresh">重试</button></p>
      <template v-else-if="tab === '作业记录' && jobs">
        <p class="work-message">{{ jobs.items.length ? "按创建时间倒序，最多显示最近 100 项。" : "当前范围没有作业记录。" }}</p>
        <div v-if="jobs.items.length" class="table-scroll"><table><thead><tr><th>作业</th><th>数据周期</th><th>开始时间</th><th>状态</th><th>进度 / 错误</th></tr></thead>
          <tbody><tr v-for="j in jobs.items" :key="j.job_id"><td :title="j.job_id">{{ j.job_name }}</td><td>{{ j.period || "—" }}</td><td>{{ j.started_at || "—" }}</td>
            <td>{{ statusLabels[j.status] || j.status }}</td><td class="reason-cell">{{ j.err_msg || j.err_code || j.progress + "%" }}</td></tr></tbody></table></div>
      </template>
      <template v-else-if="tab === '数据质量' && quality">
        <dl class="quality-metrics"><div><dt>原始航班</dt><dd>{{ number(quality.rows.ods) }}</dd></div><div><dt>清洗后航班</dt><dd>{{ number(quality.rows.dwd) }}</dd></div>
          <div><dt>数据保留率</dt><dd>{{ quality.rows.kept_rate == null ? "—" : percent(quality.rows.kept_rate, 3) + "%" }}</dd></div></dl>
        <div class="quality-reasons"><div v-for="(reasons,title) in {'近期作业剔除记录': quality.drop_reasons, '近期作业质量标记': quality.flag_reasons}" :key="title">
          <h3>{{ title }}</h3><p v-if="!Object.keys(reasons).length">没有记录</p><p v-for="(count, reason) in reasons" :key="reason">{{ reason }} · {{ number(count) }}</p>
        </div></div>
        <p class="work-message">本月文件接入审计 · 最近 {{ quality.audits.length }} 条</p>
        <div v-if="quality.audits.length" class="table-scroll"><table><thead><tr><th>文件</th><th>状态</th><th>剔除行数</th><th>错误码</th></tr></thead>
          <tbody><tr v-for="a in quality.audits" :key="a.id"><td>{{ a.file_name }}</td><td>{{ statusLabels[a.status] || a.status }}</td><td>{{ number(a.drop_rows) }}</td><td>{{ a.err_code || "—" }}</td></tr></tbody></table></div>
      </template>
      <template v-else-if="tab === '失败文件' && failures">
        <p class="work-message">{{ failures.items.length ? "最近 100 条未忽略的失败记录" : "没有待排查的失败文件。" }}</p>
        <div v-if="failures.items.length" class="table-scroll"><table><thead><tr><th>周期</th><th>数据集 / 文件</th><th>尝试次数</th><th>失败原因</th></tr></thead>
          <tbody><tr v-for="f in failures.items" :key="f.id"><td>{{ f.period }}</td><td>{{ f.dataset }} · {{ f.file_name }}</td><td>{{ f.attempts }}</td><td class="reason-cell">{{ f.err_msg || f.err_code }}</td></tr></tbody></table></div>
      </template>
      <template v-else-if="tab === '模型验证' && report">
        <div class="report-intro"><h3>{{ report.current ? "上线模型留出测试" : "历史模型留出测试" }}</h3>
          <p>{{ report.model_ver }} · {{ report.holdout.test_range.join(" 至 ") }} · {{ number(report.holdout.full.support) }} 班</p></div>
        <div class="table-scroll"><table><thead><tr><th>输入条件</th><th>AUC</th><th>召回率</th><th>精确率</th><th>F1</th><th>判定阈值</th></tr></thead>
          <tbody><tr v-for="(m,label) in {'数仓特征':report.holdout.full, '屏蔽前序航班':report.holdout.no_chain}" :key="label">
            <td>{{ label }}</td><td>{{ metric(m.auc) }}</td><td>{{ percent(m.recall) }}%</td><td>{{ percent(m.precision) }}%</td><td>{{ metric(m.f1) }}</td><td>{{ percent(m.threshold) }}%</td></tr></tbody></table></div>
        <div class="report-intro"><h3>滚动时间回测</h3><p>每个窗口用前两个月训练，下一月选择阈值，再用随后一个月测试。两种输入共用相同航班与阈值，仅改变前序航班特征。</p></div>
        <div class="table-scroll"><table><thead><tr><th>训练月份</th><th>验证月</th><th>测试月</th><th>测试航班</th><th>数仓 AUC / 召回率</th><th>缺链 AUC / 召回率</th></tr></thead>
          <tbody><tr v-for="fold in report.folds" :key="fold.test"><td>{{ fold.train.join(" 至 ") }}</td><td>{{ fold.validation }}</td><td>{{ fold.test }}</td><td>{{ number(fold.full.support) }}</td>
            <td>{{ metric(fold.full.auc) }} / {{ percent(fold.full.recall) }}%</td><td>{{ metric(fold.no_chain.auc) }} / {{ percent(fold.no_chain.recall) }}%</td></tr></tbody></table></div>
      </template>
    </Card>
  </template>
</template>
