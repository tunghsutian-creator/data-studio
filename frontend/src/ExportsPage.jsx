import { useEffect, useState } from "react";
import {
  ArrowClockwise,
  CheckCircle,
  Clock,
  FolderOpen,
  Package,
  SpinnerGap,
  Stack,
  WarningCircle,
} from "@phosphor-icons/react";
import { loadCollections, loadExports } from "./api.js";

const ACTIVE_STATUSES = new Set(["QUEUED", "RUNNING", "VERIFYING"]);
const statusLabels = {
  QUEUED: "等待处理",
  RUNNING: "正在写出",
  VERIFYING: "正在校验",
  COMPLETED: "已完成",
  FAILED: "已安全停止",
  CANCELLED: "已取消",
};

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (bytes >= 1024 ** 3) return (bytes / 1024 ** 3).toFixed(2) + " GB";
  if (bytes >= 1024 ** 2) return (bytes / 1024 ** 2).toFixed(2) + " MB";
  if (bytes >= 1024) return (bytes / 1024).toFixed(2) + " KB";
  return bytes.toLocaleString("zh-CN") + " B";
}

function formatDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function EmptyState({ icon: Icon, title, description }) {
  return <div className="export-empty"><Icon size={27} /><strong>{title}</strong><span>{description}</span></div>;
}

export function ExportsPage() {
  const [snapshot, setSnapshot] = useState({ collections: [], exports: [], loading: true, error: "" });
  const [retryKey, setRetryKey] = useState(0);

  useEffect(() => {
    let active = true;
    let timer;
    const controller = new AbortController();
    const refresh = async () => {
      try {
        const [collections, exports] = await Promise.all([loadCollections({ signal: controller.signal }), loadExports({ signal: controller.signal })]);
        if (!active) return;
        setSnapshot({ collections, exports, loading: false, error: "" });
        if (exports.some((item) => ACTIVE_STATUSES.has(item.status))) {
          timer = window.setTimeout(refresh, 1000);
        }
      } catch (error) {
        if (active) setSnapshot((current) => ({ ...current, loading: false, error: error.message || "无法读取导出记录" }));
      }
    };
    refresh();
    return () => { active = false; controller.abort(); window.clearTimeout(timer); };
  }, [retryKey]);

  return (
    <div className="secondary-page exports-page">
      <header className="page-header">
        <div><span>可复现数据交付</span><h1>集合与导出</h1><p>命名集合固定明确的文件 UUID；导出记录保留模式、哈希校验状态和本机结果位置。</p></div>
        <button className="button button-secondary" type="button" disabled={snapshot.loading} onClick={() => { setSnapshot((current) => ({ ...current, loading: true })); setRetryKey((value) => value + 1); }}><ArrowClockwise size={17} />刷新</button>
      </header>

      {snapshot.error ? <section className="export-history-error"><WarningCircle size={20} /><span><strong>无法读取集合与导出记录</strong><small>{snapshot.error}</small></span></section> : null}
      {snapshot.loading ? <div className="export-page-loading"><SpinnerGap className="spin" size={27} />正在读取本地记录</div> : (
        <div className="export-page-grid">
          <section className="export-history-panel">
            <header><span><Stack size={19} /><strong>命名集合</strong></span><small>{snapshot.collections.length} 个</small></header>
            <div className="collection-list">
              {snapshot.collections.length ? snapshot.collections.map((collection) => (
                <article className="collection-card" key={collection.id}>
                  <span className="collection-icon"><Stack size={19} weight="duotone" /></span>
                  <div><strong>{collection.name}</strong><small>{collection.purpose || "未填写用途"}</small><span>{Number(collection.asset_count || 0).toLocaleString("zh-CN")} 个文件 · {formatBytes(collection.total_bytes)} · revision {collection.revision}</span></div>
                </article>
              )) : <EmptyState icon={Stack} title="还没有命名集合" description="在导出预检中可把精确文件快照保存为集合。" />}
            </div>
          </section>

          <section className="export-history-panel">
            <header><span><Package size={19} /><strong>导出记录</strong></span><small>{snapshot.exports.length} 条</small></header>
            <div className="export-history-list">
              {snapshot.exports.length ? snapshot.exports.map((item) => {
                const activeExport = ACTIVE_STATUSES.has(item.status);
                const failed = item.status === "FAILED";
                return (
                  <article className="export-history-row" key={item.id}>
                    <span className={failed ? "export-history-icon is-failed" : activeExport ? "export-history-icon is-active" : "export-history-icon"}>{failed ? <WarningCircle size={20} weight="fill" /> : activeExport ? <SpinnerGap className="spin" size={20} /> : <CheckCircle size={20} weight="fill" />}</span>
                    <div className="export-history-main"><strong>{item.name}</strong><small>{item.export_mode} · {item.duplicate_policy} · {Number(item.file_count || 0).toLocaleString("zh-CN")} 个文件 · {formatBytes(item.total_bytes)}</small>{item.archive_path ? <code><FolderOpen size={13} />{item.archive_path}</code> : null}{item.error_detail ? <p>{item.error_detail}</p> : null}</div>
                    <div className="export-history-state"><span className={`export-status is-${String(item.status || "unknown").toLowerCase()}`}>{statusLabels[item.status] || item.status}</span><small><Clock size={12} />{formatDate(item.finished_at || item.created_at)}</small></div>
                  </article>
                );
              }) : <EmptyState icon={Package} title="还没有导出记录" description="从数据库选择数据集或单个文件后开始安全导出。" />}
            </div>
          </section>
        </div>
      )}
    </div>
  );
}
