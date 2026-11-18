import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  server: {
    proxy: { "/api": {
      target: loadEnv(mode, ".", "AIRTRACE_").AIRTRACE_BACKEND_URL || "http://127.0.0.1:5000",
      changeOrigin: true,
    } },
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (
            id.includes("/node_modules/echarts/") ||
            id.includes("/node_modules/zrender/")
          )
            return "charts";
          if (
            id.includes("/node_modules/motion-") ||
            id.includes("/node_modules/framer-motion/")
          )
            return "motion";
        },
      },
    },
  },
}));
