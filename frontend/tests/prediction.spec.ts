import { expect, test } from "@playwright/test";

test("real model evaluates the selected plan and invalidates edited results", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("机场范围", { exact: true }).selectOption("SFO");
  await page.getByRole("button", { name: "延误预测", exact: true }).click();
  await expect(page.getByRole("combobox", { name: "出发机场", exact: true })).toHaveValue("SFO");
  await page.getByLabel("起飞日期").fill("2024-12-21");
  await page.getByLabel("计划起飞时间").fill("17:40");
  await page.getByRole("combobox", { name: "航空公司", exact: true }).selectOption("UA");
  const reply = page.waitForResponse(r => r.url().endsWith("/api/v1/predict") && r.request().method() === "POST");
  await page.getByRole("button", { name: "运行预测" }).click();
  const response = await reply;
  expect(response.request().postDataJSON()).toMatchObject({
    origin: "SFO", dest: "LAX", airline: "UA", flight_date: "2024-12-21", crs_dep_time: "1740",
  });
  const { data } = await response.json();
  expect(["lightgbm", "baseline"]).toContain(data.engine);
  await expect(page.getByTestId("prediction-probability")).toHaveText((100 * data.prediction.prob_delay).toFixed(1) + "%");
  await expect(page.getByText(data.explanation.factors[0].name_cn, { exact: true })).toBeVisible();
  await page.getByText("模型与输入来源").click();
  await expect(page.getByText(data.model_ver, { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/prediction-desktop.png" });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: "test-results/prediction-mobile.png", fullPage: true });
  await page.getByRole("combobox", { name: "到达机场", exact: true }).selectOption("SFO");
  await expect(page.getByTestId("prediction-probability")).toHaveCount(0);
  await page.getByRole("button", { name: "运行预测" }).click();
  await expect(page.getByRole("alert")).toContainText("出发和到达机场相同");
});

test("prediction handles service failure, retry, demo mode and an obsolete response", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "延误预测", exact: true }).click();
  await page.getByLabel("数据来源", { exact: true }).selectOption("demo");
  await expect(page.getByRole("button", { name: "运行预测" })).toHaveCount(0);
  await page.getByRole("button", { name: "切换到数仓数据" }).click();
  const submit = page.getByRole("button", { name: "运行预测" });
  await page.route("**/api/v1/predict", route => route.fulfill({
    status: 503, json: { code: 521, message: "模型服务暂时不可用" },
  }), { times: 1 });
  await submit.click();
  await expect(page.getByRole("alert")).toHaveText("模型服务暂时不可用");
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/v1/predict", async route => {
    await gate;
    await route.fulfill({ status: 503, json: { code: 521, message: "已过期的错误" } }).catch(() => {});
  }, { times: 1 });
  const request = page.waitForRequest(req => req.url().endsWith("/api/v1/predict"));
  await submit.click();
  await request;
  await page.getByLabel("起飞日期").fill("2015-01-18");
  release();
  await submit.click();
  await expect(page.getByTestId("prediction-probability")).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("rankings show localized airport names across months and share labels with the filter", async ({ page }) => {
  await page.goto("/");
  const rows = page.locator(".rank-row");
  await expect(rows.first()).toContainText("关岛国际机场");
  await expect(rows.nth(1)).toContainText("俄勒冈西南地区机场");
  await page.getByLabel("统计月份", { exact: true }).fill("2024-12");
  await expect(page.locator(".ranking .tag")).toHaveText("2024-12");
  for (const label of await rows.locator(".rank-info strong > span").allTextContents()) {
    expect(label).toMatch(/[\u3400-\u9fff]/);
  }
  await page.getByLabel("机场范围", { exact: true }).selectOption("ORD");
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("芝加哥奥黑尔国际机场");
  await expect(page.locator("select[aria-label='机场范围'] option:checked")).toHaveText(/芝加哥奥黑尔国际机场/);
});
