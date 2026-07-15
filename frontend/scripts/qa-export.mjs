import { chromium } from "playwright-core";

const edge = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const baseUrl = process.env.ACADEMIC_VAULT_QA_URL || "http://127.0.0.1:4173/";

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function dataset(index) {
  const serial = String(index).padStart(2, "0");
  return {
    id: `dataset-${serial}`,
    name: `QA 数据集 ${serial}`,
    canonical_name: `QA 数据集 ${serial}`,
    workstream: index % 2 ? "D_PA" : "REFERENCE",
    material_state: index % 3 ? "VIRGIN" : "RECYCLED",
    modality: index % 2 ? "FTIR" : "TENSILE",
    status: "REVIEW",
    sample_code: `S-${serial}`,
    asset_count: 1,
    size_bytes: 1024 * index,
    confidence: 0.75,
    extension: index % 2 ? "csv" : "txt",
    updated_at: "2026-07-15T10:00:00Z",
    sha256: "a".repeat(64),
    hash_verified: true,
    evidence: ["Synthetic browser QA record"],
    assets: [{ id: `asset-${serial}`, name: `sample-${serial}.csv`, size_bytes: 1024 * index }],
  };
}

const records = Array.from({ length: 25 }, (_, index) => dataset(index + 1));
const browser = await chromium.launch({
  executablePath: edge,
  headless: true,
  args: ["--disable-gpu"],
});
const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 });
const consoleErrors = [];
const failedRequests = [];
const previewRequests = [];
let createRequest = null;
let exportPolls = 0;

page.on("console", (message) => {
  if (message.type() === "error") consoleErrors.push(message.text());
});
page.on("requestfailed", (request) => {
  failedRequests.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText || "failed"}`);
});

await page.route("**/api/**", async (route) => {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  const json = (payload, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(payload) });

  if (path === "/api/summary") return json({ datasets: 25, review: 25, storage: "325 KB", high: 0, medium: 25, low: 0, ingestedThisMonth: 0 });
  if (path === "/api/filters") return json({ workstreams: ["D_PA", "REFERENCE"], material_states: ["VIRGIN", "RECYCLED"], modalities: ["FTIR", "TENSILE"], extensions: ["CSV", "TXT"] });
  if (path === "/api/jobs") return json({ items: [], total: 0 });
  if (path === "/api/rules") return json({ items: [], total: 0 });
  if (path === "/api/config") return json({ ai_enabled: false, auto_scan_seconds: 0, auto_accept_enabled: false });
  if (path === "/api/ai/health") return json({ enabled: false, worker_running: false, provider_reachable: false });
  if (/^\/api\/datasets\/[^/]+\/ai$/.test(path)) return json({ items: [], total: 0 });

  if (path === "/api/datasets") {
    const limit = Number(url.searchParams.get("limit") || 20);
    const offset = Number(url.searchParams.get("offset") || 0);
    const query = (url.searchParams.get("query") || "").toLocaleLowerCase("zh-CN");
    const filtered = query ? records.filter((item) => item.name.toLocaleLowerCase("zh-CN").includes(query)) : records;
    return json({ items: filtered.slice(offset, offset + limit), total: filtered.length, limit, offset });
  }

  const detailMatch = path.match(/^\/api\/datasets\/(dataset-\d+)$/);
  if (detailMatch) return json(records.find((item) => item.id === detailMatch[1]));

  if (path === "/api/exports/preview" && request.method() === "POST") {
    const payload = request.postDataJSON();
    previewRequests.push(payload);
    const assetCount = payload.dataset_ids?.length || records.length;
    return json({
      selection_token: "qa-selection-token-" + "x".repeat(40),
      expires_at: "2099-01-01T00:00:00Z",
      catalog_revision: 7,
      selection_sha256: "b".repeat(64),
      selection_kind: payload.filter ? "FILTER" : "DATASETS",
      ready: true,
      asset_count: assetCount,
      total_bytes: assetCount * 1024,
      issues: { counts: { DUPLICATE_SHA256: 1 }, blocking_codes: [] },
      items: [],
    });
  }

  if (path === "/api/exports" && request.method() === "POST") {
    createRequest = request.postDataJSON();
    return json({ id: "export-qa-1", status: "QUEUED", archive_path: null, error_detail: null }, 202);
  }

  if (path === "/api/exports/export-qa-1") {
    exportPolls += 1;
    const completed = exportPolls >= 2;
    return json({
      id: "export-qa-1",
      status: completed ? "COMPLETED" : "RUNNING",
      archive_path: completed ? "C:\\qa-only\\exports\\Academic-Vault-QA" : null,
      error_detail: null,
    });
  }

  return json({});
});

try {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByRole("heading", { name: "数据库概览" }).waitFor();
  assert(await page.locator("tbody tr").count() === 20, "first page should contain 20 datasets");

  await page.getByRole("button", { name: "选择 QA 数据集 01" }).click();
  await page.getByRole("button", { name: "第 2 页" }).click();
  await page.getByRole("button", { name: "选择 QA 数据集 21" }).click();
  await page.getByText("已选择 2 个数据集").waitFor();
  assert((await page.locator(".selection-bar").innerText()).includes("约 2 个文件"), "selection metadata should survive pagination");

  await page.getByRole("button", { name: "预检并导出" }).click();
  await page.getByText("可以安全导出").waitFor();
  assert(previewRequests.length === 1, "explicit selection should request exactly one preview");
  assert(JSON.stringify(previewRequests[0]) === JSON.stringify({ dataset_ids: ["dataset-01", "dataset-21"] }), "preview should contain the two exact sorted dataset IDs");
  assert(!(await page.getByRole("dialog").innerText()).includes("qa-selection-token"), "selection token must not be rendered");

  await page.getByRole("button", { name: "开始导出" }).click();
  await page.getByText("导出与校验已完成").waitFor({ timeout: 5000 });
  assert(createRequest?.selection_token?.startsWith("qa-selection-token-"), "export should use the opaque preview token");
  assert(!Object.keys(createRequest || {}).some((key) => key.includes("path") || key.includes("root")), "browser must not submit an output path or root");
  await page.getByRole("button", { name: "完成" }).click();
  await page.getByRole("button", { name: "清空" }).click();

  await page.getByRole("button", { name: "第 1 页" }).click();
  await page.getByRole("button", { name: "选择当前筛选全部结果" }).click();
  await page.getByText("已选择 25 个数据集").waitFor();
  await page.getByRole("button", { name: "预检并导出" }).click();
  await page.getByText("可以安全导出").waitFor();
  assert(JSON.stringify(previewRequests.at(-1)) === JSON.stringify({ filter: {} }), "filtered selection should send a normalized filter without pagination fields");
  await page.getByRole("dialog").getByRole("button", { name: "取消", exact: true }).click();

  const search = page.getByPlaceholder("全局搜索（项目 / 样品 / 文件名 / 关键字 / SHA-256）");
  await search.fill("QA 数据集");
  await page.locator(".selection-bar").waitFor({ state: "detached" });

  await page.getByRole("button", { name: "选择 QA 数据集 01" }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(150);
  const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
  assert(!horizontalOverflow, "mobile export selection should not create document-level horizontal overflow");
  assert(await page.getByRole("button", { name: "预检并导出" }).isVisible(), "mobile export action should remain accessible");
  assert(consoleErrors.length === 0, `browser console errors: ${consoleErrors.join(" | ")}`);
  assert(failedRequests.length === 0, `failed browser requests: ${failedRequests.join(" | ")}`);

  console.log(JSON.stringify({
    passed: true,
    explicitSelection: previewRequests[0],
    filteredSelection: previewRequests[1],
    exportMode: createRequest.export_mode,
    duplicatePolicy: createRequest.duplicate_policy,
    exportPolls,
    horizontalOverflow,
    consoleErrors,
    failedRequests,
  }, null, 2));
} finally {
  await browser.close();
}
