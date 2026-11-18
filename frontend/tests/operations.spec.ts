import { expect, test } from "@playwright/test";

test("ranking uses executed samples and drills into matching delayed flights", async ({ page }) => {
  await page.goto("/");
  const first = page.locator(".rank-row").first();
  await expect(first).toContainText("GUM");
  await expect(first).toContainText("15 / 31");
  await page.getByLabel("最低执行架次").fill("100");
  await expect(first).not.toContainText("GUM");
  await page.getByLabel("最低执行架次").fill("0");
  await first.click();
  await page.getByRole("button", { name: "查看 GUM 延误航班" }).click();
  await expect(page.getByRole("heading", { name: "航班明细" })).toBeVisible();
  await expect(page.getByText(/共 15 班/)).toBeVisible();
  const table = page.locator("tbody tr");
  await expect(table).toHaveCount(15);
  expect((await table.locator("td:nth-child(3)").allTextContents()).every(v => v === "GUM")).toBe(true);
  expect((await table.locator("td:nth-child(7)").allTextContents()).every(v => Number(v) >= 15)).toBe(true);
  await page.getByRole("combobox", { name: "航班状态" }).selectOption("");
  await expect(page.getByText(/共 31 班/)).toBeVisible();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(table).toHaveCount(11);
  await page.getByRole("combobox", { name: "到达机场" }).selectOption("GUM");
  await expect(page.getByText("当前条件下没有航班。")).toBeVisible();
  await page.getByRole("combobox", { name: "到达机场" }).selectOption("");
  await expect(page.getByText(/共 31 班 · 第 1 页/)).toBeVisible();
  await page.screenshot({ path: "test-results/flight-search-desktop.png" });
  const dl = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出本页" }).click();
  expect((await dl).suggestedFilename()).toContain("第1页");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("authenticated operations exposes monthly jobs, quality, failures and paired backtests", async ({ page }) => {
  const password = process.env.AIRTRACE_TEST_ADMIN_PASSWORD;
  test.skip(!password, "需要本机管理测试账号");
  await page.goto("/");
  await page.getByRole("button", { name: "延误预测", exact: true }).click();
  await page.getByRole("button", { name: "查看模型验证" }).click();
  await page.getByLabel("应用账号").fill(process.env.AIRTRACE_TEST_ADMIN_USER || "admin");
  await page.getByLabel("应用密码").fill(password!);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "上线模型留出测试" })).toBeVisible();
  await expect(page.getByText("屏蔽前序航班", { exact: true })).toBeVisible();
  expect(await page.locator("table").nth(1).locator("tbody tr").count()).toBeGreaterThan(1);
  await page.screenshot({ path: "test-results/model-validation.png" });
  await page.getByRole("button", { name: "作业记录", exact: true }).click();
  await page.getByRole("combobox", { name: "作业范围" }).selectOption("month");
  await expect(page.locator("tbody tr").first()).toContainText("2015-01");
  await page.getByRole("button", { name: "数据质量", exact: true }).click();
  await expect(page.locator(".quality-metrics")).toContainText("469,968");
  await page.getByLabel("报告月份").fill("2024-12");
  await expect(page.locator(".quality-metrics")).not.toContainText("469,968");
  await expect(page.locator(".quality-metrics")).toBeVisible();
  await page.getByRole("button", { name: "失败文件", exact: true }).click();
  await expect(page.getByText(/没有待排查的失败文件|最近 100 条未忽略/)).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByLabel("应用账号")).toBeVisible();
  await expect(page.getByRole("button", { name: "失败文件", exact: true })).toHaveCount(0);
});

test("monitoring role can read quality but cannot read failed files", async ({ page, request }) => {
  const password = process.env.AIRTRACE_TEST_OPS_PASSWORD;
  test.skip(!password, "需要本机监控测试账号");
  await page.goto("/");
  await page.getByRole("button", { name: "数据管理", exact: true }).click();
  await page.getByLabel("应用账号").fill(process.env.AIRTRACE_TEST_OPS_USER || "ops");
  await page.getByLabel("应用密码").fill(password!);
  const login = page.waitForResponse(r => r.url().endsWith("/auth/login"));
  await page.getByRole("button", { name: "登录", exact: true }).click();
  const { data } = await (await login).json();
  await page.getByRole("button", { name: "数据质量", exact: true }).click();
  await expect(page.locator(".quality-metrics")).toContainText("469,968");
  await expect(page.getByRole("button", { name: "失败文件", exact: true })).toHaveCount(0);
  const blocked = await request.get("/api/v1/ingest/failed", { headers: { Authorization: "Bearer " + data.token } });
  expect(blocked.status()).toBe(403);
  await page.route("**/api/v1/quality/report?**", route => route.fulfill({
    status: 401, json: { code: 401, message: "测试会话已过期" },
  }));
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("会话已过期");
  await expect(page.locator(".quality-metrics")).toHaveCount(0);
});
