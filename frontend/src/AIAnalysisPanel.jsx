import { useCallback, useEffect, useState } from "react";
import {
  Brain,
  CheckCircle,
  ClockCountdown,
  Cpu,
  Play,
  SpinnerGap,
  WarningCircle,
} from "@phosphor-icons/react";
import { loadAIHealth, loadDatasetAI, requestAIAnalysis } from "./api.js";

const ACTIVE_STATUSES = new Set(["QUEUED", "RUNNING", "RETRY_WAIT"]);
const STATUS_LABELS = {
  QUEUED: "等待中",
  RUNNING: "分析中",
  RETRY_WAIT: "等待重试",
  COMPLETED: "建议已生成",
  ABSTAINED: "证据不足",
  FAILED: "分析失败",
  CANCELLED: "已取消",
};

function shortVersion(value, length = 10) {
  const text = String(value || "—");
  return text.length > length ? text.slice(0, length) + "…" : text;
}

function AIStatusBadge({ health }) {
  if (!health) return <span className="ai-health is-loading">检查中</span>;
  if (!health.enabled) return <span className="ai-health is-disabled">未启用</span>;
  if (health.available && health.worker_running) return <span className="ai-health is-online">本地在线</span>;
  return <span className="ai-health is-offline">模型离线</span>;
}

function AISuggestion({ task }) {
  const run = task?.runs?.[0];
  const suggestion = run?.classification;
  const model = run?.model;
  if (!task) {
    return <p className="ai-empty">尚无本地 AI 运行记录。仅在需要时手动分析，不会自动接受结果。</p>;
  }
  if (ACTIVE_STATUSES.has(task.status)) {
    return (
      <div className="ai-progress" role="status">
        <SpinnerGap className="spin" size={17} />
        <span><strong>{STATUS_LABELS[task.status]}</strong><small>第 {task.attempt_count}/{task.max_attempts} 次尝试</small></span>
      </div>
    );
  }
  if (!suggestion) {
    return (
      <div className="ai-error" role="status">
        <WarningCircle size={17} weight="fill" />
        <span><strong>{task.last_error_code || STATUS_LABELS[task.status] || "分析失败"}</strong><small>{task.last_error_detail || "未产生可用建议"}</small></span>
      </div>
    );
  }

  const confidence = Math.round(Number(suggestion.confidence || 0) * 100);
  return (
    <div className="ai-suggestion">
      <div className="ai-suggestion-head">
        <span><CheckCircle size={17} weight="fill" /><strong>{suggestion.modality}</strong></span>
        <b>{confidence}%</b>
      </div>
      <dl>
        <div><dt>项目</dt><dd>{suggestion.workstream || "UNASSIGNED"}</dd></div>
        <div><dt>样品</dt><dd>{suggestion.sample_id || "未识别"}</dd></div>
        <div><dt>建议名称</dt><dd title={suggestion.proposed_name || ""}>{suggestion.proposed_name || "未建议"}</dd></div>
        <div><dt>审核</dt><dd>{suggestion.needs_review ? "必须人工审核" : "仍需用户确认"}</dd></div>
      </dl>
      <ul className="ai-evidence">
        {(suggestion.evidence || []).map((item, index) => (
          <li key={item.kind + item.value + index}><span>{item.kind}</span>{item.value}</li>
        ))}
      </ul>
      {suggestion.abstain_reason ? <p className="ai-abstain">{suggestion.abstain_reason}</p> : null}
      {model ? (
        <div className="ai-version" title={`${model.model_id} · ${model.model_revision}`}>
          <Cpu size={14} />
          <span>{model.model_id} · {model.quantization}</span>
          <small>runtime {model.runtime_release} · prompt {shortVersion(model.prompt_version)} · taxonomy {shortVersion(model.taxonomy_version)} · schema {model.output_schema_version}</small>
        </div>
      ) : null}
    </div>
  );
}

export function AIAnalysisPanel({ datasetId, onToast }) {
  const [snapshot, setSnapshot] = useState({ datasetId: "", health: null, tasks: [], loading: true, error: "" });
  const [refreshKey, setRefreshKey] = useState(0);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let active = true;
    let timer = 0;
    let controller = null;

    async function refresh() {
      controller = new AbortController();
      const [healthResult, historyResult] = await Promise.allSettled([
        loadAIHealth({ signal: controller.signal }),
        loadDatasetAI(datasetId, { signal: controller.signal }),
      ]);
      if (!active) return;
      const historyLoaded = historyResult.status === "fulfilled";
      const nextTasks = historyLoaded ? historyResult.value : [];
      setSnapshot((current) => {
        const sameDataset = current.datasetId === datasetId;
        return {
          datasetId,
          health: healthResult.status === "fulfilled" ? healthResult.value : (sameDataset ? current.health : null),
          tasks: historyLoaded ? nextTasks : (sameDataset ? current.tasks : []),
          loading: false,
          error: historyLoaded ? "" : "无法读取本地 AI 任务状态",
        };
      });
      const hasActiveTask = nextTasks.some((task) => ACTIVE_STATUSES.has(task.status));
      timer = window.setTimeout(refresh, historyLoaded && hasActiveTask ? 1000 : 10000);
    }

    refresh();
    return () => {
      active = false;
      window.clearTimeout(timer);
      controller?.abort();
    };
  }, [datasetId, refreshKey]);

  const current = snapshot.datasetId === datasetId
    ? snapshot
    : { datasetId, health: null, tasks: [], loading: true, error: "" };
  const latestTask = current.tasks[0] || null;
  const taskIsActive = Boolean(latestTask && ACTIVE_STATUSES.has(latestTask.status));
  const canAnalyze = Boolean(current.health?.enabled && current.health?.worker_running);

  const analyze = useCallback(async () => {
    setSubmitting(true);
    try {
      const task = await requestAIAnalysis(datasetId);
      onToast(task.created === false ? "相同输入已在本地 AI 队列中" : "已加入本地 AI 分析队列");
      setRefreshKey((value) => value + 1);
    } catch (error) {
      onToast("AI 分析未启动：" + error.message);
    } finally {
      setSubmitting(false);
    }
  }, [datasetId, onToast]);

  return (
    <section className="inspector-section ai-panel" aria-live="polite">
      <header>
        <div><Brain size={17} /><h2>本地 AI 建议</h2><AIStatusBadge health={current.health} /></div>
        <button
          className="ai-run-button"
          type="button"
          disabled={!canAnalyze || submitting || taskIsActive}
          onClick={analyze}
        >
          {submitting ? <SpinnerGap className="spin" size={14} /> : taskIsActive ? <ClockCountdown size={14} /> : <Play size={14} weight="fill" />}
          {taskIsActive ? STATUS_LABELS[latestTask.status] : latestTask ? "重新分析" : "开始分析"}
        </button>
      </header>
      {current.loading ? <div className="ai-progress"><SpinnerGap className="spin" size={17} /><span><strong>正在读取</strong><small>仅连接本机服务</small></span></div> : null}
      {!current.loading && current.error ? <p className="ai-load-error">{current.error}</p> : null}
      {!current.loading && !current.error ? <AISuggestion task={latestTask} /> : null}
      <p className="ai-safety-note"><WarningCircle size={14} />AI 只有建议权：不会修改分类、接受数据或执行文件操作。</p>
    </section>
  );
}
