import { shallowRef } from "vue";

export interface LoginSession {
  token: string;
  role_name: string;
  user: { username: string; role: string };
}
// 只在当前页面内存中持有登录凭据，切换分析模块时保留会话。
export const session = shallowRef<LoginSession | null>(null);
