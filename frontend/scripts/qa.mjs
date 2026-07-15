import { chromium } from "playwright-core";
import path from "node:path";
import { fileURLToPath } from "node:url";

const edge = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const here = path.dirname(fileURLToPath(import.meta.url));
const design = path.resolve(here, "..", "..", "design");
const browser = await chromium.launch({
  executablePath: edge,
  headless: true,
  args: ["--disable-gpu"],
});

const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 1 });
const consoleErrors = [];
const failedRequests = [];
page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("requestfailed", (request) => {
  failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText || "failed"}`);
});

await page.goto("http://127.0.0.1:8765/", { waitUntil: "networkidle" });
await page.getByRole("heading", { name: "数据库概览" }).waitFor();
const desktop = {
  title: await page.title(),
  heading: await page.getByRole("heading", { name: "数据库概览" }).innerText(),
  datasetRows: await page.locator("tbody tr").count(),
  bodyHasRealCount: (await page.locator("body").innerText()).includes("452"),
  bodyHasRealStorage: (await page.locator("body").innerText()).includes("2.29 GB"),
  bodyHasRealReviewCount: (await page.locator("body").innerText()).includes("待审核\n2"),
  bodyHasProject: (await page.locator("body").innerText()).includes("D-PA"),
  selectedVisibleRow: await page.locator("tbody tr.is-active").count() === 1,
  detailHasSha256: (await page.locator(".hash-field > span").innerText()) !== "尚未计算",
  detailHasEvidence: !(await page.locator(".inspector").innerText()).includes("暂无可解释分类证据"),
  horizontalOverflow: await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth),
};
await page.screenshot({ path: path.join(design, "qa-desktop.png"), fullPage: true });

await page.getByRole("button", { name: "下一页" }).click();
desktop.pagination = await page.getByRole("button", { name: "第 2 页" }).getAttribute("aria-current") === "page";
await page.getByRole("button", { name: "第 1 页" }).click();

const search = page.getByPlaceholder("全局搜索（项目 / 样品 / 文件名 / 关键字 / SHA-256）");
await search.fill("屏幕截图");
await page.waitForTimeout(250);
desktop.filteredRows = await page.locator("tbody tr").count();
desktop.filteredTotal = await page.locator(".table-footer > span").first().innerText();
await search.fill("");

await page.getByRole("button", { name: /导入数据/ }).click();
await page.getByRole("dialog").waitFor();
desktop.importDialog = await page.getByRole("dialog").isVisible();
await page.getByRole("button", { name: "关闭对话框" }).click();

await page.getByRole("button", { name: /待审核/ }).click();
await page.getByRole("heading", { name: "待审核", exact: true }).waitFor();
desktop.reviewRoute = page.url().endsWith("#/review");
desktop.reviewRows = await page.locator("tbody tr").count();
await page.getByRole("button", { name: /分类规则/ }).click();
await page.getByRole("heading", { name: "分类规则", exact: true }).waitFor();
desktop.lockedBuiltinRules = await page.locator('.rules-list input[type="checkbox"]:disabled').count();
await page.getByRole("button", { name: /数据库/ }).click();
await page.getByRole("heading", { name: "数据库概览" }).waitFor();

await page.setViewportSize({ width: 390, height: 844 });
await page.waitForTimeout(250);
const mobile = {
  horizontalOverflow: await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth),
  headingVisible: await page.getByRole("heading", { name: "数据库概览" }).isVisible(),
  importVisible: await page.getByRole("button", { name: /导入数据/ }).isVisible(),
  navVisible: await page.locator('aside[aria-label="主导航"]').isVisible(),
};
await page.screenshot({ path: path.join(design, "qa-mobile.png"), fullPage: true });

await browser.close();
console.log(JSON.stringify({ desktop, mobile, consoleErrors, failedRequests }, null, 2));

if (
  consoleErrors.length || failedRequests.length || desktop.horizontalOverflow || mobile.horizontalOverflow ||
  !desktop.pagination || desktop.filteredRows !== 2 || desktop.reviewRows !== 2 || !desktop.lockedBuiltinRules ||
  !desktop.selectedVisibleRow || !desktop.detailHasSha256 || !desktop.detailHasEvidence
) {
  process.exitCode = 1;
}
