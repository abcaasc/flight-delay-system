import { test, expect } from "@playwright/test";

test("real warehouse: airport changes KPI, network, ranking and daily values together", async ({
  page,
  request,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/");
  await expect(page.locator(".network-map svg")).toBeVisible();
  const whole = await page.getByTestId("航班总量").innerText();
  const allPaths = await page
    .locator(".network-map svg>g:nth-child(2)>path")
    .count();
  const allTrend = await page.locator(".chart-data tbody").textContent();
  await page.getByLabel("机场范围", { exact: true }).selectOption("ORD");
  const response = await request.get(
    "/api/v1/overview?date_from=2015-01-01&date_to=2015-01-31&airports=ORD",
  );
  const body = await response.json();
  await expect(page.getByTestId("航班总量")).toContainText(
    body.data.kpi.kpi.flights.toLocaleString("zh-CN"),
  );
  await expect(page.getByTestId("航班总量")).not.toHaveText(whole);
  await expect(page.locator(".ranking .rank-row")).toHaveCount(1);
  await expect(page.locator(".ranking .rank-row")).toContainText("ORD");
  expect(await page.locator(".chart-data tbody").textContent()).not.toBe(
    allTrend,
  );
  expect(
    await page.locator(".network-map svg>g:nth-child(2)>path").count(),
  ).toBeLessThan(allPaths);
  const paths = await page
    .locator(".network-map svg>g:nth-child(2)>path title")
    .allTextContents();
  expect(paths.every((p) => p.startsWith("ORD →"))).toBe(true);
  await expect(page.locator(".insight")).toContainText("ORD");
  expect(errors).toEqual([]);
});

test("missing month clears old data; recovery restores all charts", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".network-map")).toBeVisible();
  // 数据全量入库后 2015-02 也会就绪，用明确的 521 响应稳定验证无数据恢复。
  await page.route("**/api/v1/overview?**date_from=2015-02-01**", route => route.fulfill({
    status: 503, contentType: "application/json",
    body: JSON.stringify({ code: 521, message: "测试范围内无汇总数据", data: null }),
  }));
  await page.getByLabel("统计月份", { exact: true }).fill("2015-02");
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByTestId("航班总量")).toContainText("—");
  await expect(page.locator(".network-map")).toHaveCount(0);
  await expect(page.locator(".trend-chart")).toHaveCount(0);
  await page.getByRole("button", { name: "查看 2015 年 1 月" }).click();
  await expect(page.locator(".network-map")).toBeVisible();
  await expect(page.getByTestId("航班总量")).not.toContainText("—");
});

test("demo data reaggregates by month and airport; daily totals match KPI", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("数据来源", { exact: true }).selectOption("demo");
  await expect(page.locator(".network-map")).toBeVisible();
  const before = await page.getByTestId("航班总量").innerText();
  const paths = await page
    .locator(".network-map svg>g:nth-child(2)>path title")
    .allTextContents();
  await page.getByLabel("统计月份", { exact: true }).fill("2016-02");
  await expect(page.getByTestId("航班总量")).not.toHaveText(before);
  await expect(page.locator(".chart-data tbody tr")).toHaveCount(29);
  expect(
    await page
      .locator(".network-map svg>g:nth-child(2)>path title")
      .allTextContents(),
  ).not.toEqual(paths);
  await page.getByLabel("机场范围", { exact: true }).selectOption("LAX");
  await expect(page.locator(".ranking .rank-row")).toHaveCount(1);
  const daily = await page
    .locator(".chart-data tbody tr td:nth-child(2)")
    .allTextContents();
  const total = Number(
    (
      await page.getByTestId("航班总量").locator("strong").innerText()
    ).replaceAll(",", ""),
  );
  expect(daily.reduce((s, v) => s + Number(v), 0)).toBe(total);
});

test("navigation icons, stable external ring, chart lifecycle, download", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".network-map")).toBeVisible();
  await expect(page.locator("nav button>svg:first-child")).toHaveCount(6);
  const initialBox = await page
    .getByRole("button", { name: "航线网络", exact: true })
    .boundingBox();
  await page.getByRole("button", { name: "航线网络", exact: true }).click();
  const active = page.locator("nav button.active");
  await expect(active).toHaveCSS("outline-offset", "3px");
  await expect(active).toHaveCSS("outline-style", "solid");
  expect(await active.boundingBox()).toEqual(initialBox);
  await page.getByRole("button", { name: "多维分析", exact: true }).click();
  await expect(page.locator(".trend-chart canvas")).toBeVisible();
  await page.getByRole("button", { name: "运行总览", exact: true }).click();
  await expect(page.locator(".trend-chart canvas")).toBeVisible();
  await page.getByLabel("趋势分析指标").selectOption("avg_delay_min");
  await expect(page.locator(".trend-chart")).toHaveAttribute(
    "aria-label",
    /每日平均延误/,
  );
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "导出分析" }).click();
  expect((await download).suggestedFilename()).toContain("数仓-2015-01");
});

for (const width of [1920, 1024, 768, 390])
  test(`responsive ${width}: no document overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/");
    await expect(page.locator(".network-map")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const filterBox = await page.getByLabel("机场范围", { exact: true }).boundingBox();
    const refreshBox = await page.getByRole("button", { name: "刷新分析数据" }).boundingBox();
    expect(filterBox!.x + filterBox!.width).toBeLessThanOrEqual(refreshBox!.x);
    await page.screenshot({
      path: `test-results/dashboard-${width}.png`,
      fullPage: true,
    });
  });

test("full-screen overview fits the desktop viewport and exits cleanly", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto("/");
  await expect(page.locator(".network-map")).toBeVisible();
  await page.getByRole("button", { name: "进入大屏", exact: true }).click();
  await expect(page.locator(".overview-screen")).toBeVisible();
  await expect(page.locator(".sidebar")).not.toBeVisible();
  await expect(page.locator(".trend-chart canvas")).toBeVisible();
  const size = await page.evaluate(() => ({
    body: document.documentElement.scrollHeight,
    viewport: innerHeight,
  }));
  expect(size.body).toBeLessThanOrEqual(size.viewport + 1);
  await page.screenshot({ path: "test-results/dashboard-fullscreen.png" });
  await page.getByRole("button", { name: "退出大屏", exact: true }).click();
  await expect(page.locator(".sidebar")).toBeVisible();
});

test("slow obsolete responses cannot overwrite the newest airport selection", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".network-map")).toBeVisible();
  await page.route("**/api/v1/overview?**airports=ORD", async (route) => {
    const response = await route.fetch();
    await new Promise((r) => setTimeout(r, 500));
    try {
      await route.fulfill({ response });
    } catch {
      /* canceled by the newer request */
    }
  });
  await page.getByLabel("机场范围", { exact: true }).selectOption("ORD");
  await page.getByLabel("机场范围", { exact: true }).selectOption("LAX");
  await expect(page.locator(".ranking .rank-row")).toHaveCount(1);
  await expect(page.locator(".ranking .rank-row")).toContainText("LAX");
  const observed = await page.getByTestId("航班总量").textContent();
  await page.waitForTimeout(650);
  expect(await page.getByTestId("航班总量").textContent()).toBe(observed);
  await expect(page.locator(".ranking .rank-row")).toContainText("LAX");
});
