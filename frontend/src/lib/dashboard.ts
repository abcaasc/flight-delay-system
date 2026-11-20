export interface Kpi {
  flights: number;
  operated: number;
  delayed: number;
  cancelled: number;
  ontime_rate: number | null;
  delay_rate: number | null;
  cancel_rate: number | null;
  avg_delay_min: number | null;
  airports: number;
}
export interface AirportNode {
  id: string;
  name: string;
  city?: string | null;
  lat: number | null;
  lon: number | null;
  flights: number;
  operated: number;
  delayed: number;
  delay_rate: number | null;
  ontime_rate: number | null;
  cancel_rate: number | null;
  avg_delay_min: number | null;
}
export interface RouteLink {
  source: string;
  target: string;
  flights: number;
  delay_rate: number | null;
  avg_delay_min: number | null;
}
export interface TrendPoint {
  bucket: string;
  flights: number;
  delay_rate: number | null;
  ontime_rate: number | null;
  avg_delay_min: number | null;
}
export interface DashboardData {
  kpi: Kpi;
  nodes: AirportNode[];
  links: RouteLink[];
  trend: TrendPoint[];
  source: "api" | "demo";
  missingMonths?: string[];
}
export interface Coverage {
  available_months: string[];
  missing_months: string[];
  sampled_months: string[];
}
export type DataSource = DashboardData["source"];
export type MapMetric = "delay_rate" | "flights";

export function monthRange(month: string) {
  if (!/^(201[5-9]|202[0-4])-(0[1-9]|1[0-2])$/.test(month))
    throw new Error("请选择 2015—2024 年的有效月份");
  const [year, m] = month.split("-").map(Number);
  return {
    date_from: `${month}-01`,
    date_to: `${month}-${new Date(Date.UTC(year!, m!, 0)).getUTCDate()}`,
  };
}
export const percent = (v: number | null | undefined, digits = 1) =>
  v == null ? "—" : (v * 100).toFixed(digits);
export const number = (v: number | null | undefined) =>
  v == null ? "—" : v.toLocaleString("zh-CN");

export class ApiError extends Error {
  constructor(public code: number, message: string) { super(message); }
}

// API 基址：默认同源相对路径，单容器部署（后端托管 dist）与 Vite 代理都能直接工作；
// 前后端分开部署（如 GitHub Pages + 独立后端）时用 VITE_API_BASE 指定完整地址。
export const API_BASE = (
  import.meta.env.VITE_API_BASE?.trim() || "/api/v1"
).replace(/\/+$/, "");

export async function apiRequest<T>(
  path: string,
  signal: AbortSignal,
  options: Pick<RequestInit, "method" | "body" | "headers"> = {},
): Promise<T> {
  const response = await fetch(`${API_BASE}/${path}`, {
    ...options,
    signal,
    cache: "no-store",
  });
  if (
    !(response.headers.get("content-type") || "").includes("application/json")
  )
    throw new Error("后端连接失败，请确认 Flask 服务已启动");
  const body = await response.json();
  if (!response.ok || body.code !== 0)
    throw new ApiError(body.code || response.status, body.message || `请求失败（${response.status}）`);
  return body.data;
}
export async function fetchDashboard(
  month: string,
  airport: string,
  signal: AbortSignal,
): Promise<DashboardData> {
  const params = new URLSearchParams({
    ...monthRange(month),
    ...(airport ? { airports: airport } : {}),
  });
  const [overview, network] = await Promise.all([
    apiRequest<{ kpi: { kpi: Kpi; coverage: Coverage }; trend: { points: TrendPoint[] } }>(
      `overview?${params}`,
      signal,
    ),
    apiRequest<{ nodes: AirportNode[]; links: RouteLink[]; coverage: Coverage }>(
      `network?${params}&top_routes=1000`,
      signal,
    ),
  ]);
  return {
    kpi: overview.kpi.kpi,
    trend: overview.trend.points,
    nodes: network.nodes,
    links: network.links,
    source: "api",
    missingMonths: [...new Set([
      ...(overview.kpi.coverage?.missing_months ?? []), ...(network.coverage?.missing_months ?? []),
    ])],
  };
}

// 演示模式使用同一组“日 × 出港航线”模拟记录聚合，保证 KPI、趋势和网络口径一致。
const locations = [
  ["SEA", "西雅图", 47.4502, -122.3088],
  ["SFO", "旧金山", 37.6213, -122.379],
  ["LAX", "洛杉矶", 33.9416, -118.4085],
  ["DEN", "丹佛", 39.8561, -104.6737],
  ["DFW", "达拉斯", 32.8998, -97.0403],
  ["ORD", "芝加哥", 41.9742, -87.9073],
  ["ATL", "亚特兰大", 33.6407, -84.4277],
  ["JFK", "纽约", 40.6413, -73.7781],
  ["MIA", "迈阿密", 25.7959, -80.287],
  ["BOS", "波士顿", 42.3656, -71.0096],
] as const;
export const demoAirports = locations.map(([id, name]) => ({ id, name }));
interface Counts {
  flights: number;
  cancelled: number;
  delayed: number;
  delaySum: number;
}
const counts = (): Counts => ({
  flights: 0,
  cancelled: 0,
  delayed: 0,
  delaySum: 0,
});
function add(a: Counts, b: Counts) {
  a.flights += b.flights;
  a.cancelled += b.cancelled;
  a.delayed += b.delayed;
  a.delaySum += b.delaySum;
}
function rates(a: Counts) {
  const operated = a.flights - a.cancelled;
  return {
    flights: a.flights,
    operated,
    cancelled: a.cancelled,
    delayed: a.delayed,
    delay_rate: operated ? a.delayed / operated : null,
    ontime_rate: operated ? 1 - a.delayed / operated : null,
    cancel_rate: a.flights ? a.cancelled / a.flights : null,
    avg_delay_min: operated ? a.delaySum / operated : null,
  };
}
export function demoDashboard(month: string, airport: string): DashboardData {
  const range = monthRange(month),
    days = Number(range.date_to.slice(-2));
  const seed = Number(month.replace("-", ""));
  const totals = counts(),
    nodeCounts = locations.map(counts),
    daily = Array.from({ length: days }, counts);
  const routeCounts = new Map<string, Counts>();
  for (let day = 0; day < days; day++)
    for (let from = 0; from < locations.length; from++)
      for (let to = 0; to < locations.length; to++) {
        if (from === to || (from * 7 + to * 3 + seed) % 5 === 0) continue;
        const h = (seed * 17 + day * 31 + from * 47 + to * 23) % 997;
        const flights = 10 + (h % 65),
          cancelled = Math.floor((flights * (h % 5)) / 100);
        const delayed = Math.floor(
          (flights - cancelled) * (0.09 + (h % 31) / 100),
        );
        const entry = {
          flights,
          cancelled,
          delayed,
          delaySum:
            delayed * (28 + (h % 35)) - (flights - cancelled - delayed) * 3,
        };
        add(nodeCounts[from]!, entry);
        if (airport && locations[from]![0] !== airport) continue;
        add(totals, entry);
        add(daily[day]!, entry);
        const key = `${locations[from]![0]}-${locations[to]![0]}`;
        const r = routeCounts.get(key) || counts();
        add(r, entry);
        routeCounts.set(key, r);
      }
  return {
    source: "demo",
    kpi: { ...rates(totals), airports: airport ? 1 : locations.length },
    nodes: locations.map(([id, name, lat, lon], i) => ({
      id,
      name,
      city: name,
      lat,
      lon,
      ...rates(nodeCounts[i]!),
    })),
    links: [...routeCounts].map(([key, value]) => ({
      source: key.split("-")[0]!,
      target: key.split("-")[1]!,
      ...rates(value),
    })),
    trend: daily.map((value, i) => ({
      bucket: `${month}-${String(i + 1).padStart(2, "0")}`,
      ...rates(value),
    })),
  };
}

export function downloadCsv(
  rows: (string | number | null)[][],
  filename: string,
) {
  const cell = (value: string | number | null) => {
    let s = value == null ? "" : String(value);
    if (typeof value === "string" && /^[\s]*[=+@-]/.test(s)) s = `'${s}`;
    return `"${s.replaceAll('"', '""')}"`;
  };
  const blob = new Blob(
    ["\uFEFF" + rows.map((row) => row.map(cell).join(",")).join("\r\n")],
    { type: "text/csv;charset=utf-8" },
  );
  const url = URL.createObjectURL(blob),
    a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
