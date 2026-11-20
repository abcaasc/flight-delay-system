import { expect, test } from "@playwright/test";

test("demo recovers using the last available month", async ({ page }) => {
  await page.goto("/");
  const month = page.getByLabel("统计月份", { exact: true });
  await month.fill("2024-12");
  await expect(page.getByTestId("航班总量")).toContainText("590,581");
  await month.fill("2015-01");
  await expect(page.getByRole("alert")).toContainText(/无汇总数据|无网络数据/);
  await page.getByRole("button", { name: "查看 2024 年 12 月", exact: true }).click();
  await expect(month).toHaveValue("2024-12");
  await expect(page.getByTestId("航班总量")).toContainText("590,581");
});

test("demo distinguishes missing details and sampled flights", async ({ page }) => {
  await page.goto("/");
  const month = page.getByLabel("统计月份", { exact: true });
  await month.fill("2024-11");
  await expect(page.getByTestId("航班总量")).toContainText("575,404");
  await page.getByRole("button", { name: "航班查询", exact: true }).click();
  await expect(page.getByText("该月份暂无逐班明细，请切换月份。")).toBeVisible();
  await month.fill("2024-12");
  await expect(page.getByText("本月显示抽样航班明细，查询结果和架次仅代表样本。")).toBeVisible();
  await expect(page.getByText("共 80,000 班 · 第 1 页")).toBeVisible();
});
