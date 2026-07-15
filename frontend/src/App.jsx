import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { DatabasePage } from "./Catalog.jsx";
import { EditDatasetDialog, ImportDialog, NewRuleDialog } from "./Dialogs.jsx";
import { Inspector } from "./Inspector.jsx";
import { AppShell } from "./Shell.jsx";
import { IngestPage, ReviewPage, RulesPage, SettingsPage } from "./SecondaryPages.jsx";
import {
  acceptDataset,
  createRule,
  deferDataset,
  loadDatasetDetail,
  loadWorkspace,
  patchRule,
  saveConfig,
  startScan,
  updateDataset,
} from "./api.js";
import {
  filterOptions as seedFilterOptions,
  seedConfig,
  seedDatasets,
  seedJobs,
  seedRules,
  seedSummary,
} from "./seed.js";

const routes = new Set(["database", "review", "ingest", "rules", "settings"]);
const defaultFilters = {
  project: "全部项目",
  materialState: "全部",
  modality: "全部",
  dateFrom: "",
  dateTo: "",
  format: "全部",
};

const modalityLabels = {
  SEM: "SEM",
  TENSILE: "拉伸",
  FTIR: "FTIR",
  RHEOLOGY: "流变",
  IMPACT: "冲击",
  GPC: "GPC",
  TORQUE: "扭矩",
  OPTICAL: "光学图像",
  UNKNOWN: "未知",
};
const materialLabels = { VIRGIN: "干燥态", RECYCLED: "回收料", UNKNOWN: "未知" };

function routeFromLocation() {
  const route = window.location.hash.replace(/^#\/?/, "");
  return routes.has(route) ? route : "database";
}

export function App() {
  const [activePage, setActivePage] = useState(routeFromLocation);
  const [datasets, setDatasets] = useState(seedDatasets);
  const [summary, setSummary] = useState(seedSummary);
  const [options, setOptions] = useState(seedFilterOptions);
  const [jobs, setJobs] = useState(seedJobs);
  const [rules, setRules] = useState(seedRules);
  const [config, setConfig] = useState(seedConfig);
  const [source, setSource] = useState("loading");
  const [partialFallback, setPartialFallback] = useState(false);
  const [query, setQuery] = useState("");
  const deferredQuery = useDeferredValue(query.trim().toLowerCase());
  const [filters, setFilters] = useState(defaultFilters);
  const [selectedId, setSelectedId] = useState("PBT-20240513-IMPACT-007");
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [dialog, setDialog] = useState(null);
  const [toast, setToast] = useState("");
  const searchRef = useRef(null);
  const toastTimer = useRef(null);

  const announce = useCallback((message) => {
    window.clearTimeout(toastTimer.current);
    setToast(message);
    toastTimer.current = window.setTimeout(() => setToast(""), 2600);
  }, []);

  useEffect(() => {
    let active = true;
    loadWorkspace().then((workspace) => {
      if (!active) return;
      setDatasets(workspace.datasets);
      setSummary(workspace.summary);
      setOptions(workspace.filters);
      setJobs(workspace.jobs);
      setRules(workspace.rules);
      setConfig(workspace.config);
      setSource(workspace.source);
      setPartialFallback(workspace.partialFallback);
      const initial = routeFromLocation() === "review"
        ? workspace.datasets.find((item) => item.statusCode === "review" || item.statusCode === "deferred")
        : workspace.datasets[0];
      setSelectedId(initial?.id || workspace.datasets[0]?.id || "");
    });
    return () => {
      active = false;
      window.clearTimeout(toastTimer.current);
    };
  }, []);

  useEffect(() => {
    const onHashChange = () => setActivePage(routeFromLocation());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  useEffect(() => {
    const onKeyDown = (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
      }
      if (event.key === "Escape") {
        if (dialog) setDialog(null);
        else setInspectorOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dialog]);

  const selectedDataset = useMemo(
    () => datasets.find((item) => item.id === selectedId) || datasets[0] || null,
    [datasets, selectedId],
  );

  useEffect(() => {
    if (source !== "api" || !selectedId) return undefined;
    let active = true;
    loadDatasetDetail(selectedId).then((detail) => {
      if (!active) return;
      setDatasets((current) => current.map((item) => item.id === selectedId ? { ...item, ...detail } : item));
    }).catch(() => undefined);
    return () => { active = false; };
  }, [selectedId, source]);

  const visibleRows = useMemo(() => {
    const search = deferredQuery;
    return datasets.filter((item) => {
      if (search) {
        const haystack = [item.name, item.project, item.sample, item.modalityLabel, item.originalPath, item.sha256]
          .join(" ")
          .toLowerCase();
        if (!haystack.includes(search)) return false;
      }
      if (filters.project !== "全部项目" && item.project !== filters.project) return false;
      if (filters.materialState !== "全部" && item.materialState !== filters.materialState) return false;
      if (filters.modality !== "全部" && item.modalityLabel !== filters.modality) return false;
      if (filters.format !== "全部" && item.format !== filters.format) return false;
      if (filters.dateFrom && item.date < filters.dateFrom) return false;
      if (filters.dateTo && item.date > filters.dateTo) return false;
      return true;
    });
  }, [datasets, deferredQuery, filters]);

  const navigate = useCallback((page) => {
    if (!routes.has(page)) return;
    window.location.hash = "/" + page;
    setActivePage(page);
    if (page === "database") {
      setSelectedId(datasets[0]?.id || "");
      setInspectorOpen(true);
    } else if (page === "review") {
      const firstReview = datasets.find((item) => item.statusCode === "review" || item.statusCode === "deferred");
      setSelectedId(firstReview?.id || "");
      setInspectorOpen(true);
    }
  }, [datasets]);

  const selectDataset = useCallback((id) => {
    setSelectedId(id);
    setInspectorOpen(true);
  }, []);

  const mutateDataset = useCallback((id, changes) => {
    setDatasets((current) => current.map((item) => item.id === id ? { ...item, ...changes } : item));
  }, []);

  const accept = useCallback(async (id) => {
    const item = datasets.find((dataset) => dataset.id === id);
    if (!item || item.statusCode === "ingested") {
      announce("该数据集已经入库");
      return;
    }
    mutateDataset(id, { status: "已入库", statusCode: "ingested" });
    setSummary((current) => ({ ...current, review: Math.max(0, Number(current.review || 0) - 1), ingestedThisMonth: Number(current.ingestedThisMonth || 0) + 1 }));
    setInspectorOpen(false);
    announce("分类已接受，数据集进入校验与入库队列");
    if (source === "api") acceptDataset(id).catch(() => announce("本地界面已更新，但后端提交失败，请稍后重试"));
  }, [announce, datasets, mutateDataset, source]);

  const defer = useCallback((id) => {
    mutateDataset(id, { status: "暂缓", statusCode: "deferred" });
    setInspectorOpen(false);
    announce("已暂缓处理，数据和建议均保持不变");
    if (source === "api") deferDataset(id).catch(() => announce("暂缓状态未同步到后端，请稍后重试"));
  }, [announce, mutateDataset, source]);

  const saveDatasetEdit = useCallback((draft) => {
    const changes = {
      canonicalName: draft.canonicalName,
      modality: draft.modality,
      modalityLabel: modalityLabels[draft.modality] || draft.modality,
      project: draft.project,
      workstream: draft.workstream,
      materialStateCode: draft.materialStateCode,
      materialState: materialLabels[draft.materialStateCode] || "未知",
      sample: draft.sample,
    };
    mutateDataset(draft.id, changes);
    setDialog(null);
    announce("分类建议已更新，原始文件保持不变");
    if (source === "api") updateDataset(draft.id, { ...draft, ...changes }).catch(() => announce("修改未同步到后端，请稍后重试"));
  }, [announce, mutateDataset, source]);

  const runScan = useCallback(async (scanSource) => {
    if (source === "api") {
      const result = await startScan(scanSource);
      const job = result?.job || result;
      if (job?.id) setJobs((current) => [job, ...current]);
      return { ...job, review: job?.review || 3 };
    }
    return { detected: scanSource === "inbox" ? 18 : 822, review: scanSource === "inbox" ? 3 : 37 };
  }, [source]);

  const toggleRule = useCallback(async (id) => {
    const currentRule = rules.find((rule) => rule.id === id);
    if (!currentRule || currentRule.source !== "user") {
      announce("内置规则不可停用或修改");
      return;
    }
    const nextEnabled = !currentRule.enabled;
    setRules((current) => current.map((rule) => rule.id === id ? { ...rule, enabled: nextEnabled } : rule));
    if (source !== "api") {
      announce("用户规则状态已更新");
      return;
    }
    try {
      const saved = await patchRule(id, { enabled: nextEnabled });
      setRules((current) => current.map((rule) => rule.id === id ? { ...rule, ...saved } : rule));
      announce("用户规则状态已保存");
    } catch (error) {
      setRules((current) => current.map((rule) => rule.id === id ? { ...rule, enabled: currentRule.enabled } : rule));
      announce("规则更新失败：" + error.message);
    }
  }, [announce, rules, source]);

  const addRule = useCallback(async (draft) => {
    const temporaryId = "rule-local-" + Date.now();
    const optimisticRule = {
      ...draft,
      id: temporaryId,
      description: "Python regex：" + draft.pattern,
      scope: modalityLabels[draft.label] || draft.label,
      version: "v1",
      source: "user",
      matches: null,
    };
    setRules((current) => [optimisticRule, ...current]);
    setDialog(null);
    if (source !== "api") {
      announce("用户规则已创建，将在下一次扫描时生效");
      return;
    }
    try {
      const saved = await createRule(draft);
      setRules((current) => current.map((rule) => rule.id === temporaryId ? saved : rule));
      announce("用户规则已保存，将在下一次扫描时生效");
    } catch (error) {
      setRules((current) => current.filter((rule) => rule.id !== temporaryId));
      announce("规则创建失败：" + error.message);
    }
  }, [announce, source]);

  const saveSettings = useCallback((draft) => {
    setConfig(draft);
    announce("设置已保存到本地工作站");
    if (source === "api") saveConfig(draft).catch(() => announce("设置未同步到后端，请稍后重试"));
  }, [announce, source]);

  let page;
  if (activePage === "review") {
    page = <ReviewPage rows={visibleRows} selectedId={selectedId} onSelect={selectDataset} />;
  } else if (activePage === "ingest") {
    page = <IngestPage jobs={jobs} onImport={() => setDialog("import")} />;
  } else if (activePage === "rules") {
    page = <RulesPage rules={rules} onToggle={toggleRule} onAdd={() => setDialog("rule")} />;
  } else if (activePage === "settings") {
    page = <SettingsPage config={config} onSave={saveSettings} />;
  } else {
    page = (
      <DatabasePage
        summary={summary}
        rows={visibleRows}
        total={visibleRows.length}
        selectedId={selectedId}
        onSelect={selectDataset}
        filters={filters}
        options={options}
        onFilter={(field, value) => setFilters((current) => ({ ...current, [field]: value }))}
        onReset={() => setFilters(defaultFilters)}
      />
    );
  }

  const showInspector = activePage === "database" || activePage === "review";
  return (
    <>
      <AppShell
        activePage={activePage}
        onNavigate={navigate}
        query={query}
        onQuery={setQuery}
        searchRef={searchRef}
        source={source}
        partialFallback={partialFallback}
        onImport={() => setDialog("import")}
        inspectorOpen={inspectorOpen}
        onCloseInspector={() => setInspectorOpen(false)}
        toast={toast}
        inspector={showInspector ? (
          <Inspector
            dataset={selectedDataset}
            onClose={() => setInspectorOpen(false)}
            onAccept={accept}
            onEdit={() => setDialog("edit")}
            onDefer={defer}
            onToast={announce}
          />
        ) : null}
      >
        {page}
      </AppShell>
      {dialog === "import" ? <ImportDialog onClose={() => setDialog(null)} onStartScan={runScan} /> : null}
      {dialog === "edit" ? <EditDatasetDialog dataset={selectedDataset} onClose={() => setDialog(null)} onSave={saveDatasetEdit} /> : null}
      {dialog === "rule" ? <NewRuleDialog onClose={() => setDialog(null)} onSave={addRule} /> : null}
    </>
  );
}
