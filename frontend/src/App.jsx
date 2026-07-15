import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { DatabasePage, SelectionBar } from "./Catalog.jsx";
import { EditDatasetDialog, ExportDialog, ImportDialog, NewRuleDialog } from "./Dialogs.jsx";
import { Inspector } from "./Inspector.jsx";
import { AppShell } from "./Shell.jsx";
import { IngestPage, ReviewPage, RulesPage, SettingsPage } from "./SecondaryPages.jsx";
import {
  acceptDataset,
  createRule,
  deferDataset,
  loadCatalogPage,
  loadDatasetDetail,
  loadSummary,
  loadWorkspace,
  patchRule,
  saveConfig,
  startScan,
  updateDataset,
} from "./api.js";

const routes = new Set(["database", "review", "ingest", "rules", "settings"]);
const defaultFilters = {
  project: "全部项目",
  materialState: "全部",
  modality: "全部",
  dateFrom: "",
  dateTo: "",
  format: "全部",
};
const emptySummary = { datasets: 0, review: 0, storage: "—", high: 0, medium: 0, low: 0, ingestedThisMonth: 0 };
const emptyOptions = { projects: ["全部项目"], materialStates: ["全部"], modalities: ["全部"], formats: ["全部"] };
const emptyConfig = { retainSource: true, verifySha256: true, autoScan: false, model: "rules-only", reviewPolicy: "manual" };

const modalityLabels = {
  SEM: "SEM", TENSILE: "拉伸", FTIR: "FTIR", RHEOLOGY: "流变", IMPACT: "冲击",
  GPC: "GPC", TORQUE: "扭矩", OPTICAL: "光学图像", SIMULATION: "模拟", UNKNOWN: "未知",
};
const materialLabels = { VIRGIN: "干燥态", RECYCLED: "回收料", UNKNOWN: "未知" };
const workstreamLabels = {
  REFERENCE: "参考资料", PA_ADR_RECYCLE: "PA ADR 回收料", D_PA: "D-PA", UDC: "UDC",
  VITRIMER: "VITRIMER", UNASSIGNED: "未分类项目", UNKNOWN: "未知项目",
};
const reverseModality = Object.fromEntries(Object.entries(modalityLabels).map(([code, label]) => [label, code]));
const reverseMaterial = Object.fromEntries(Object.entries(materialLabels).map(([code, label]) => [label, code]));
const reverseWorkstream = Object.fromEntries(Object.entries(workstreamLabels).map(([code, label]) => [label, code]));

function routeFromLocation() {
  const route = window.location.hash.replace(/^#\/?/, "");
  return routes.has(route) ? route : "database";
}

function initialCatalogState() {
  const params = new URLSearchParams(window.location.search);
  const pageSize = [20, 50].includes(Number(params.get("limit"))) ? Number(params.get("limit")) : 20;
  return {
    query: params.get("query") || "",
    page: Math.max(1, Number(params.get("page")) || 1),
    pageSize,
    filters: {
      project: params.get("project") || defaultFilters.project,
      materialState: params.get("material_state") || defaultFilters.materialState,
      modality: params.get("modality") || defaultFilters.modality,
      dateFrom: params.get("date_from") || "",
      dateTo: params.get("date_to") || "",
      format: params.get("extension") || defaultFilters.format,
    },
  };
}

function filterCode(value, allValue, reverse) {
  if (!value || value === allValue) return undefined;
  return reverse[value] || value;
}

export function App() {
  const initialRef = useRef(null);
  if (initialRef.current === null) initialRef.current = initialCatalogState();
  const initial = initialRef.current;
  const [activePage, setActivePage] = useState(routeFromLocation);
  const [catalog, setCatalog] = useState({ items: [], total: 0, loading: true, error: "" });
  const [summary, setSummary] = useState(emptySummary);
  const [options, setOptions] = useState(emptyOptions);
  const [jobs, setJobs] = useState([]);
  const [rules, setRules] = useState([]);
  const [config, setConfig] = useState(emptyConfig);
  const [source, setSource] = useState("loading");
  const [partialFailure, setPartialFailure] = useState(false);
  const [query, setQuery] = useState(initial.query);
  const deferredQuery = useDeferredValue(query.trim());
  const [filters, setFilters] = useState(initial.filters);
  const [page, setPage] = useState(initial.page);
  const [pageSize, setPageSize] = useState(initial.pageSize);
  const [retryKey, setRetryKey] = useState(0);
  const [selectedId, setSelectedId] = useState("");
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [dialog, setDialog] = useState(null);
  const [pendingAction, setPendingAction] = useState("");
  const [toast, setToast] = useState("");
  const [selectedDatasets, setSelectedDatasets] = useState(() => new Map());
  const [selectedAssets, setSelectedAssets] = useState(() => new Map());
  const [allFilteredSelected, setAllFilteredSelected] = useState(false);
  const [exportDialogSelection, setExportDialogSelection] = useState(null);
  const searchRef = useRef(null);
  const toastTimer = useRef(null);
  const datasets = catalog.items;
  const selectedDatasetIds = useMemo(() => new Set(selectedDatasets.keys()), [selectedDatasets]);
  const selectedAssetIds = useMemo(() => new Set(selectedAssets.keys()), [selectedAssets]);

  const announce = useCallback((message) => {
    window.clearTimeout(toastTimer.current);
    setToast(message);
    toastTimer.current = window.setTimeout(() => setToast(""), 3200);
  }, []);

  useEffect(() => {
    let active = true;
    loadWorkspace().then((workspace) => {
      if (!active) return;
      setSummary(workspace.summary);
      setOptions(workspace.filters);
      setJobs(workspace.jobs);
      setRules(workspace.rules);
      setConfig(workspace.config);
      setPartialFailure(workspace.partialFailure);
    });
    return () => {
      active = false;
      window.clearTimeout(toastTimer.current);
    };
  }, []);

  const catalogParams = useMemo(() => ({
    limit: pageSize,
    offset: (page - 1) * pageSize,
    sort: "updated_at",
    order: "desc",
    query: deferredQuery || undefined,
    status: activePage === "review" ? "REVIEW" : undefined,
    workstream: filterCode(filters.project, "全部项目", reverseWorkstream),
    material_state: filterCode(filters.materialState, "全部", reverseMaterial),
    modality: filterCode(filters.modality, "全部", reverseModality),
    extension: filters.format === "全部" ? undefined : filters.format,
    date_from: filters.dateFrom || undefined,
    date_to: filters.dateTo || undefined,
  }), [activePage, deferredQuery, filters, page, pageSize]);

  useEffect(() => {
    const controller = new AbortController();
    setCatalog((current) => ({ ...current, loading: true, error: "" }));
    loadCatalogPage(catalogParams, { signal: controller.signal }).then((result) => {
      setCatalog({ ...result, loading: false, error: "" });
      setSource("api");
      setSelectedId((current) => result.items.some((item) => item.id === current) ? current : (result.items[0]?.id || ""));
    }).catch((error) => {
      if (controller.signal.aborted) return;
      setCatalog({ items: [], total: 0, loading: false, error: error.message || "无法连接本地数据库" });
      setSelectedId("");
      setSource("offline");
    });
    return () => controller.abort();
  }, [catalogParams, retryKey]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (query.trim()) params.set("query", query.trim());
    if (page !== 1) params.set("page", String(page));
    if (pageSize !== 20) params.set("limit", String(pageSize));
    if (filters.project !== defaultFilters.project) params.set("project", filters.project);
    if (filters.materialState !== defaultFilters.materialState) params.set("material_state", filters.materialState);
    if (filters.modality !== defaultFilters.modality) params.set("modality", filters.modality);
    if (filters.dateFrom) params.set("date_from", filters.dateFrom);
    if (filters.dateTo) params.set("date_to", filters.dateTo);
    if (filters.format !== defaultFilters.format) params.set("extension", filters.format);
    const next = window.location.pathname + (params.size ? "?" + params.toString() : "") + window.location.hash;
    window.history.replaceState(null, "", next);
  }, [filters, page, pageSize, query]);

  useEffect(() => {
    const onHashChange = () => {
      setActivePage(routeFromLocation());
      setPage(1);
      setAllFilteredSelected(false);
    };
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
        if (dialog) {
          setDialog(null);
          if (dialog === "export") setExportDialogSelection(null);
        }
        else setInspectorOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [dialog]);

  const selectedDataset = useMemo(
    () => datasets.find((item) => item.id === selectedId) || null,
    [datasets, selectedId],
  );

  useEffect(() => {
    if (source !== "api" || !selectedId) return undefined;
    let active = true;
    loadDatasetDetail(selectedId).then((detail) => {
      if (!active) return;
      setCatalog((current) => ({ ...current, items: current.items.map((item) => item.id === selectedId ? { ...item, ...detail } : item) }));
    }).catch(() => undefined);
    return () => { active = false; };
  }, [selectedId, source]);

  const navigate = useCallback((nextPage) => {
    if (!routes.has(nextPage)) return;
    window.location.hash = "/" + nextPage;
    setActivePage(nextPage);
    setPage(1);
    setAllFilteredSelected(false);
    setInspectorOpen(nextPage === "database" || nextPage === "review");
  }, []);

  const selectDataset = useCallback((id) => {
    setSelectedId(id);
    setInspectorOpen(true);
  }, []);

  const toggleDatasetForExport = useCallback((row) => {
    if (allFilteredSelected) {
      announce("已选择当前筛选的全部结果；请先清空后再逐项选择");
      return;
    }
    const adding = !selectedDatasetIds.has(row.id);
    setSelectedDatasets((current) => {
      const next = new Map(current);
      if (next.has(row.id)) next.delete(row.id);
      else next.set(row.id, { fileCount: row.fileCount, sizeBytes: row.sizeBytes });
      return next;
    });
    if (adding) {
      setSelectedAssets((current) => {
        const next = new Map(current);
        for (const [assetId, item] of next) {
          if (item.datasetId === row.id) next.delete(assetId);
        }
        return next;
      });
    }
  }, [allFilteredSelected, announce, selectedDatasetIds]);

  const toggleCurrentPageForExport = useCallback(() => {
    if (allFilteredSelected) {
      setAllFilteredSelected(false);
      return;
    }
    const allOnPage = datasets.length > 0 && datasets.every((row) => selectedDatasetIds.has(row.id));
    setSelectedDatasets((current) => {
      const next = new Map(current);
      datasets.forEach((row) => allOnPage ? next.delete(row.id) : next.set(row.id, { fileCount: row.fileCount, sizeBytes: row.sizeBytes }));
      return next;
    });
    if (!allOnPage) {
      const pageDatasetIds = new Set(datasets.map((row) => row.id));
      setSelectedAssets((current) => {
        const next = new Map(current);
        for (const [assetId, item] of next) {
          if (pageDatasetIds.has(item.datasetId)) next.delete(assetId);
        }
        return next;
      });
    }
  }, [allFilteredSelected, datasets, selectedDatasetIds]);

  const toggleAssetForExport = useCallback((dataset, asset) => {
    if (allFilteredSelected || selectedDatasetIds.has(dataset.id)) {
      announce("该文件已由数据集或筛选选择包含");
      return;
    }
    setSelectedAssets((current) => {
      const next = new Map(current);
      if (next.has(asset.id)) next.delete(asset.id);
      else next.set(asset.id, { datasetId: dataset.id, sizeBytes: asset.sizeBytes });
      return next;
    });
  }, [allFilteredSelected, announce, selectedDatasetIds]);

  const selectCurrentFilterForExport = useCallback(() => {
    setSelectedDatasets(new Map());
    setSelectedAssets(new Map());
    setAllFilteredSelected(true);
  }, []);

  const clearExportSelection = useCallback(() => {
    setSelectedDatasets(new Map());
    setSelectedAssets(new Map());
    setAllFilteredSelected(false);
  }, []);

  const mutateDataset = useCallback((id, changes) => {
    setCatalog((current) => ({ ...current, items: current.items.map((item) => item.id === id ? { ...item, ...changes } : item) }));
  }, []);

  const refreshSummary = useCallback(async () => {
    try { setSummary(await loadSummary()); } catch { setPartialFailure(true); }
  }, []);

  const accept = useCallback(async (id) => {
    if (source !== "api") return announce("后端离线，未执行接受操作");
    setPendingAction("accept:" + id);
    try {
      const saved = await acceptDataset(id);
      mutateDataset(id, saved);
      await refreshSummary();
      setInspectorOpen(false);
      announce("分类已接受，受管副本校验完成");
    } catch (error) {
      announce("接受失败：" + error.message);
    } finally {
      setPendingAction("");
    }
  }, [announce, mutateDataset, refreshSummary, source]);

  const defer = useCallback(async (id) => {
    if (source !== "api") return announce("后端离线，未执行暂缓操作");
    setPendingAction("defer:" + id);
    try {
      const saved = await deferDataset(id);
      mutateDataset(id, saved);
      await refreshSummary();
      setInspectorOpen(false);
      announce("已暂缓处理，数据和建议均保持不变");
    } catch (error) {
      announce("暂缓失败：" + error.message);
    } finally {
      setPendingAction("");
    }
  }, [announce, mutateDataset, refreshSummary, source]);

  const saveDatasetEdit = useCallback(async (draft) => {
    if (source !== "api") return announce("后端离线，修改未保存");
    setPendingAction("edit:" + draft.id);
    try {
      const saved = await updateDataset(draft.id, draft);
      mutateDataset(draft.id, saved);
      setDialog(null);
      announce("分类建议已保存，原始文件保持不变");
    } catch (error) {
      announce("修改失败：" + error.message);
    } finally {
      setPendingAction("");
    }
  }, [announce, mutateDataset, source]);

  const runScan = useCallback(async (scanSource) => {
    if (source !== "api") throw new Error("后端离线");
    const job = await startScan(scanSource);
    if (job?.id) setJobs((current) => [job, ...current]);
    return job;
  }, [source]);

  const toggleRule = useCallback(async (id) => {
    const currentRule = rules.find((rule) => rule.id === id);
    if (!currentRule || currentRule.source !== "user") return announce("内置规则不可停用或修改");
    if (source !== "api") return announce("后端离线，规则未修改");
    try {
      const saved = await patchRule(id, { enabled: !currentRule.enabled });
      setRules((current) => current.map((rule) => rule.id === id ? { ...rule, ...saved } : rule));
      announce("用户规则状态已保存");
    } catch (error) {
      announce("规则更新失败：" + error.message);
    }
  }, [announce, rules, source]);

  const addRule = useCallback(async (draft) => {
    if (source !== "api") return announce("后端离线，规则未创建");
    try {
      const saved = await createRule(draft);
      setRules((current) => [saved, ...current]);
      setDialog(null);
      announce("用户规则已保存，将在下一次扫描时生效");
    } catch (error) {
      announce("规则创建失败：" + error.message);
    }
  }, [announce, source]);

  const saveSettings = useCallback(async (draft) => {
    if (source !== "api") return announce("后端离线，设置未保存");
    setPendingAction("settings");
    try {
      const saved = await saveConfig(draft);
      setConfig(saved);
      announce("设置已保存到本地工作站");
    } catch (error) {
      announce("设置保存失败：" + error.message);
    } finally {
      setPendingAction("");
    }
  }, [announce, source]);

  const updateFilter = useCallback((field, value) => {
    setFilters((current) => ({ ...current, [field]: value }));
    setPage(1);
    setAllFilteredSelected(false);
  }, []);
  const updateQuery = useCallback((value) => { setQuery(value); setPage(1); setAllFilteredSelected(false); }, []);
  const resetFilters = useCallback(() => { setFilters(defaultFilters); setPage(1); setAllFilteredSelected(false); }, []);
  const retryCatalog = useCallback(() => setRetryKey((value) => value + 1), []);

  const exportSelection = useMemo(() => {
    if (allFilteredSelected) {
      const filter = {
        search: catalogParams.query,
        status: catalogParams.status,
        workstream: catalogParams.workstream,
        material_state: catalogParams.material_state,
        modality: catalogParams.modality,
        extension: catalogParams.extension,
        date_from: catalogParams.date_from,
        date_to: catalogParams.date_to,
      };
      return { filter: Object.fromEntries(Object.entries(filter).filter(([, value]) => value !== undefined && value !== "")) };
    }
    const explicit = {};
    const datasetIds = Array.from(selectedDatasets.keys()).sort();
    const assetIds = Array.from(selectedAssets.keys()).sort();
    if (datasetIds.length) explicit.dataset_ids = datasetIds;
    if (assetIds.length) explicit.asset_ids = assetIds;
    return explicit;
  }, [allFilteredSelected, catalogParams, selectedAssets, selectedDatasets]);

  const selectedFileCount = useMemo(
    () => Array.from(selectedDatasets.values()).reduce((total, item) => total + Number(item.fileCount || 0), 0) + selectedAssets.size,
    [selectedAssets, selectedDatasets],
  );
  const selectedDatasetCount = allFilteredSelected ? catalog.total : selectedDatasets.size;
  const selectedAssetCount = selectedAssets.size;

  const openExportDialog = useCallback(() => {
    setExportDialogSelection(exportSelection);
    setDialog("export");
  }, [exportSelection]);

  const closeExportDialog = useCallback(() => {
    setDialog(null);
    setExportDialogSelection(null);
  }, []);

  const tableProps = {
    rows: datasets,
    total: catalog.total,
    selectedId,
    onSelect: selectDataset,
    page,
    pageSize,
    onPageChange: setPage,
    onPageSizeChange: (size) => { setPageSize(size); setPage(1); },
    loading: catalog.loading,
    error: catalog.error,
    onRetry: retryCatalog,
    selectedDatasetIds,
    allFilteredSelected,
    onToggleDataset: toggleDatasetForExport,
    onTogglePage: toggleCurrentPageForExport,
    onSelectFiltered: selectCurrentFilterForExport,
  };

  let pageContent;
  if (activePage === "review") {
    pageContent = <ReviewPage {...tableProps} />;
  } else if (activePage === "ingest") {
    pageContent = <IngestPage jobs={jobs} onImport={() => setDialog("import")} />;
  } else if (activePage === "rules") {
    pageContent = <RulesPage rules={rules} onToggle={toggleRule} onAdd={() => setDialog("rule")} />;
  } else if (activePage === "settings") {
    pageContent = <SettingsPage config={config} onSave={saveSettings} saving={pendingAction === "settings"} />;
  } else {
    pageContent = <DatabasePage summary={summary} {...tableProps} filters={filters} options={options} onFilter={updateFilter} onReset={resetFilters} />;
  }

  const showInspector = activePage === "database" || activePage === "review";
  const actionsDisabled = source !== "api" || Boolean(pendingAction);
  return (
    <>
      <AppShell
        activePage={activePage}
        onNavigate={navigate}
        query={query}
        onQuery={updateQuery}
        searchRef={searchRef}
        source={source}
        partialFailure={partialFailure}
        onImport={() => setDialog("import")}
        importDisabled={source !== "api"}
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
            actionsDisabled={actionsDisabled}
            selectedAssetIds={selectedAssetIds}
            datasetIncludedInExport={allFilteredSelected || selectedDatasetIds.has(selectedDataset?.id)}
            onToggleAssetForExport={toggleAssetForExport}
          />
        ) : null}
      >
        {pageContent}
      </AppShell>
      {showInspector && (selectedDatasetCount > 0 || selectedAssetCount > 0) ? <SelectionBar datasetCount={selectedDatasetCount} assetCount={selectedAssetCount} fileCount={selectedFileCount} allFiltered={allFilteredSelected} onClear={clearExportSelection} onExport={openExportDialog} /> : null}
      {dialog === "import" ? <ImportDialog onClose={() => setDialog(null)} onStartScan={runScan} /> : null}
      {dialog === "edit" ? <EditDatasetDialog dataset={selectedDataset} onClose={() => setDialog(null)} onSave={saveDatasetEdit} saving={pendingAction.startsWith("edit:")} /> : null}
      {dialog === "rule" ? <NewRuleDialog onClose={() => setDialog(null)} onSave={addRule} /> : null}
      {dialog === "export" && exportDialogSelection ? <ExportDialog selection={exportDialogSelection} onClose={closeExportDialog} onStarted={() => announce("导出任务已创建；关闭窗口后仍会在本地后台继续")} /> : null}
    </>
  );
}
