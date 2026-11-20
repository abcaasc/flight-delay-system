import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./tests",
  timeout: 30000,
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    channel: "chrome",
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "warehouse",
      testIgnore: "demo.spec.ts",
      use: { baseURL: process.env.AIRTRACE_TEST_BASE_URL || "http://localhost:5173" },
    },
    {
      name: "demo",
      testMatch: "demo.spec.ts",
      use: { baseURL: process.env.AIRTRACE_DEMO_TEST_URL || "http://localhost:5175" },
    },
  ],
});
