import { ref, shallowRef, watch } from "vue";
import { apiRequest } from "./dashboard";

// 地址或凭据变化时取消旧请求。加载、空结果和失败都只对应当前查询。
export function useQuery<T>(path: () => string, token: () => string = () => "") {
  const data = shallowRef<T | null>(null);
  const error = shallowRef<Error | null>(null);
  const loading = ref(false);
  const revision = ref(0);
  watch([path, token, revision], async ([url, bearer], _, onCleanup) => {
    data.value = null;
    error.value = null;
    loading.value = !!url;
    if (!url) return;
    const controller = new AbortController();
    let current = true;
    const timer = setTimeout(() => controller.abort(), 30000);
    onCleanup(() => { current = false; clearTimeout(timer); controller.abort(); });
    try {
      const value = await apiRequest<T>(url, controller.signal, {
        headers: bearer ? { Authorization: "Bearer " + bearer } : {},
      });
      if (current) data.value = value;
    } catch (e) {
      if (current) error.value = controller.signal.aborted ? new Error("读取超时，请重试。")
        : e instanceof TypeError ? new Error("无法连接后端服务，请重试。")
        : e instanceof Error ? e : new Error("读取失败，请重试。");
    } finally {
      clearTimeout(timer);
      if (current) loading.value = false;
    }
  }, { immediate: true });
  return { data, error, loading, reload: () => { revision.value++; } };
}
