import {
  filterOptions,
  seedConfig,
  seedDatasets,
  seedJobs,
  seedRules,
  seedSummary,
} from "./seed.js";

const API_BASE = "/api";
const REQUEST_TIMEOUT = 10000;

function getPayloadItems(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.items || payload?.data || payload?.results || [];
}

function normalizeRule(item, index = 0) {
  const source = item.source || "rule";
  const isUser = source === "user";
  const label = String(item.label || item.scope || "UNKNOWN").toUpperCase();
  const rawVersion = item.version ?? 1;
  return {
    ...item,
    id: item.id || "rule-" + index,
    name: item.name || item.id || "未命名规则",
    pattern: item.pattern || "",
    label,
    description: isUser
      ? "Python regex：" + (item.pattern || "未设置")
      : item.description || "内置分类规则",
    scope: modalityLabel(label),
    priority: Number(item.priority ?? index + 1),
    version: isUser
      ? String(rawVersion).startsWith("v") ? String(rawVersion) : "v" + rawVersion
      : String(rawVersion || "builtin-v1"),
    enabled: item.enabled !== false,
    source,
    matches: Number.isFinite(Number(item.matches)) ? Number(item.matches) : null,
  };
}

function statusMeta(status) {
  const value = String(status || "review").toLowerCase();
  if (value === "indexed" || value === "已索引") {
    return { status: "已索引", statusCode: "indexed" };
  }
  if (["accepted", "ingested", "committed", "complete", "已入库"].includes(value)) {
    return { status: "已入库", statusCode: "ingested" };
  }
  if (["deferred", "paused", "暂缓"].includes(value)) {
    return { status: "暂缓", statusCode: "deferred" };
  }
  return { status: "待审核", statusCode: "review" };
}

function modalityLabel(value) {
  const labels = {
    TENSILE: "拉伸",
    RHEOLOGY: "流变",
    TORQUE: "扭矩",
    IMPACT: "冲击",
    OPTICAL: "光学图像",
    SIMULATION: "模拟",
  };
  return labels[value] || value || "未知";
}

function humanMaterial(value) {
  const labels = { VIRGIN: "干燥态", RECYCLED: "回收料", UNKNOWN: "未知" };
  return labels[value] || value || "未知";
}

function humanWorkstream(value) {
  const labels = {
    REFERENCE: "参考资料",
    PA_ADR_RECYCLE: "PA ADR 回收料",
    D_PA: "D-PA",
    UDC: "UDC",
    UNKNOWN: "未知项目",
  };
  return labels[value] || value || "未分类项目";
}

function normalizeFilters(payload) {
  const projects = payload.projects || payload.workstreams || [];
  const materials = payload.materialStates || payload.material_states || [];
  const modalities = payload.modalities || [];
  const formats = payload.formats || payload.extensions || [];
  return {
    projects: ["全部项目", ...new Set(projects.map(humanWorkstream))],
    materialStates: ["全部", ...new Set(materials.map(humanMaterial))],
    modalities: ["全部", ...new Set(modalities.map(modalityLabel))],
    formats: ["全部", ...new Set(formats.map((value) => String(value).replace(/^\./, "").toUpperCase()))],
  };
}

function normalizeAsset(asset, index, datasetId) {
  const bytes = Number(asset.size_bytes || 0);
  const size =
    asset.size ||
    (bytes > 1024 * 1024
      ? (bytes / 1024 / 1024).toFixed(2) + " MB"
      : (bytes / 1024).toFixed(2) + " KB");
  return {
    id: asset.id || datasetId + "-asset-" + index,
    name: asset.name || asset.filename || "未命名文件",
    size,
    role: asset.role || asset.file_role || "关联文件",
  };
}

export function normalizeDataset(item, index = 0) {
  const state = statusMeta(item.status || item.decision?.status);
  const modality = item.modality || item.type || item.category || "UNKNOWN";
  const materialState = item.material_state || item.materialState || "UNKNOWN";
  const assets = item.assets || item.files || [];
  const confidence = Number(item.confidence ?? item.decision?.confidence ?? 0);
  return {
    id: item.id || item.uuid || item.name || "dataset-" + index,
    name: item.name || item.canonical_name || "未命名数据集",
    canonicalName: item.canonical_name || item.canonicalName || item.name || "未命名数据集",
    project: item.project || item.workstream_label || humanWorkstream(item.workstream),
    workstream: item.workstream || "UNKNOWN",
    sample: item.sample_code || item.sample || "未识别",
    modality,
    modalityLabel: modalityLabel(modality),
    materialState: humanMaterial(materialState),
    materialStateCode: materialState,
    fileCount: Number(item.asset_count ?? item.file_count ?? assets.length ?? 0),
    confidence: confidence > 1 ? confidence / 100 : confidence,
    ...state,
    updatedAt: item.modified_at || item.modified || item.updated_at || "—",
    date: item.acquired_at || item.date || String(item.modified_at || "").slice(0, 10),
    format: String(item.extension || item.format || "未知").replace(/^\./, "").toUpperCase(),
    originalPath: item.original_path || item.path || "—",
    sha256: item.sha256 || item.digest || "尚未计算",
    hashVerified: Boolean(item.hash_verified ?? item.integrity_verified ?? false),
    evidence: item.evidence || item.decision?.evidence || ["暂无可解释分类证据"],
    files: assets.map((asset, assetIndex) => normalizeAsset(asset, assetIndex, item.id)),
  };
}

async function request(path, { timeoutMs = REQUEST_TIMEOUT, ...options } = {}) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(API_BASE + path, {
      ...options,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...options.headers,
      },
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = "API " + response.status;
      try {
        const payload = await response.json();
        detail = payload?.detail || detail;
      } catch {
        // Keep the status-based message when the response is not JSON.
      }
      throw new Error(detail);
    }
    if (response.status === 204) return null;
    return await response.json();
  } finally {
    window.clearTimeout(timer);
  }
}

async function settle(path, fallback, transform = (value) => value) {
  try {
    const payload = await request(path);
    return { value: transform(payload), live: true };
  } catch {
    return { value: fallback, live: false };
  }
}

export async function loadWorkspace() {
  const [summary, filters, datasets, jobs, rules, config] = await Promise.all([
    settle("/summary", seedSummary),
    settle("/filters", filterOptions, normalizeFilters),
    settle("/datasets?limit=500&offset=0", seedDatasets, (payload) =>
      getPayloadItems(payload).map(normalizeDataset),
    ),
    settle("/jobs", seedJobs, getPayloadItems),
    settle("/rules", seedRules, (payload) => getPayloadItems(payload).map(normalizeRule)),
    settle("/config", seedConfig),
  ]);
  return {
    summary: { ...seedSummary, ...summary.value },
    filters: { ...filterOptions, ...filters.value },
    datasets: datasets.value.length ? datasets.value : seedDatasets,
    jobs: jobs.value.length ? jobs.value : seedJobs,
    rules: rules.value.length ? rules.value : seedRules,
    config: { ...seedConfig, ...config.value },
    source: datasets.live ? "api" : "seed",
    partialFallback: ![summary, filters, datasets, jobs, rules, config].every((item) => item.live),
  };
}

export async function loadDatasetDetail(id) {
  const payload = await request("/datasets/" + encodeURIComponent(id));
  return normalizeDataset(payload);
}

export async function acceptDataset(id) {
  return request("/datasets/" + encodeURIComponent(id) + "/accept", {
    method: "POST",
    timeoutMs: 10 * 60 * 1000,
  });
}

export async function deferDataset(id) {
  return request("/datasets/" + encodeURIComponent(id) + "/defer", { method: "POST" });
}

export async function updateDataset(id, changes) {
  return request("/datasets/" + encodeURIComponent(id), {
    method: "PUT",
    body: JSON.stringify({
      canonical_name: changes.canonicalName,
      modality: changes.modality,
      workstream: changes.workstream,
      material_state: changes.materialStateCode,
      sample_code: changes.sample,
    }),
  });
}

export async function startScan(source) {
  return request("/scan", {
    method: "POST",
    body: JSON.stringify({ source }),
  });
}

export async function saveConfig(config) {
  return request("/config", { method: "PUT", body: JSON.stringify(config) });
}

export async function createRule(rule) {
  const payload = await request("/rules", {
    method: "POST",
    body: JSON.stringify({
      name: rule.name,
      pattern: rule.pattern,
      label: rule.label,
      priority: rule.priority,
      enabled: rule.enabled,
    }),
  });
  return normalizeRule(payload);
}

export async function patchRule(id, changes) {
  const payload = await request("/rules/" + encodeURIComponent(id), {
    method: "PATCH",
    body: JSON.stringify(changes),
  });
  return normalizeRule(payload);
}
