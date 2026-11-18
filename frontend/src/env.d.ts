/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基址；留空则走同源 /api/v1（单容器部署、Vite 代理都适用） */
  readonly VITE_API_BASE?: string;
  /** 看板默认统计月份，默认 2024-12 */
  readonly VITE_DEFAULT_MONTH?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
