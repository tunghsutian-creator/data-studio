import { useEffect, useMemo } from "react";
import {
  Archive,
  ArrowCounterClockwise,
  Brain,
  CalendarBlank,
  CaretLeft,
  CaretRight,
  ChartLine,
  Check,
  Database,
  FileMagnifyingGlass,
  FileText,
  Flask,
  Funnel,
  Gauge,
  ImageSquare,
  Microscope,
  ShieldCheck,
  Square,
  WaveSine,
  Tray,
} from "@phosphor-icons/react";

function paginationTokens(currentPage, pageCount) {
  if (pageCount <= 7) {
    return Array.from({ length: pageCount }, (_, index) => index + 1);
  }
  let start = Math.max(2, currentPage - 1);
  let end = Math.min(pageCount - 1, currentPage + 1);
  if (currentPage <= 4) end = 5;
  if (currentPage >= pageCount - 3) start = pageCount - 4;
  const tokens = [1];
  if (start > 2) tokens.push("ellipsis-start");
  for (let page = start; page <= end; page += 1) tokens.push(page);
  if (end < pageCount - 1) tokens.push("ellipsis-end");
  tokens.push(pageCount);
  return tokens;
}

const modalityIcons = {
  FTIR: Flask,
  TENSILE: Gauge,
  RHEOLOGY: WaveSine,
  SEM: Microscope,
  GPC: ChartLine,
  TORQUE: Gauge,
  IMPACT: ShieldCheck,
  OPTICAL: ImageSquare,
  SIMULATION: FileText,
};

export function ConfidenceBar({ value, showNumber = true }) {
  const percent = Math.round(Number(value || 0) * 100);
  const level = percent >= 82 ? "high" : percent >= 55 ? "medium" : "low";
  return (
    <div className="confidence-cell" aria-label={"置信度 " + percent + "%"}>
      {showNumber ? <span>{percent}%</span> : null}
      <span className="confidence-track"><i className={"confidence-fill " + level} style={{ width: percent + "%" }} /></span>
    </div>
  );
}

export function StatusPill({ code, label }) {
  return <span className={"status-pill " + (code || "review")}>{label}</span>;
}

function SummaryStrip({ summary }) {
  const total = Number(summary.datasets || 0) || 1;
  const items = [
    ["数据集总数", Number(summary.datasets || 0).toLocaleString("zh-CN")],
    ["待审核", Number(summary.review || 0).toLocaleString("zh-CN")],
    ["本地存储", summary.storage || "—"],
    ["本月入库", Number(summary.ingestedThisMonth || summary.ingested_this_month || 0).toLocaleString("zh-CN")],
    ["高置信度", Number(summary.high || 0).toLocaleString("zh-CN"), Math.round((summary.high || 0) / total * 1000) / 10 + "%"],
    ["中置信度", Number(summary.medium || 0).toLocaleString("zh-CN"), Math.round((summary.medium || 0) / total * 1000) / 10 + "%"],
    ["低置信度", Number(summary.low || 0).toLocaleString("zh-CN"), Math.round((summary.low || 0) / total * 1000) / 10 + "%"],
  ];
  return (
    <section className="summary-section" aria-labelledby="database-title">
      <h1 id="database-title">数据库概览</h1>
      <div className="summary-strip">
        {items.map(([label, value, suffix]) => (
          <div className="summary-item" key={label}>
            <span>{label}</span>
            <strong>{value}{suffix ? <small>（{suffix}）</small> : null}</strong>
          </div>
        ))}
      </div>
    </section>
  );
}

function SelectField({ label, value, options, onChange }) {
  return (
    <label className="filter-field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((option) => <option key={option}>{option}</option>)}
      </select>
    </label>
  );
}

function FiltersBar({ filters, options, onChange, onReset }) {
  return (
    <form className="filters-bar" onSubmit={(event) => event.preventDefault()}>
      <SelectField label="项目" value={filters.project} options={options.projects || ["全部项目"]} onChange={(value) => onChange("project", value)} />
      <SelectField label="材料状态" value={filters.materialState} options={options.materialStates || ["全部"]} onChange={(value) => onChange("materialState", value)} />
      <SelectField label="测试类型" value={filters.modality} options={options.modalities || ["全部"]} onChange={(value) => onChange("modality", value)} />
      <label className="filter-field date-filter">
        <span>日期范围</span>
        <span className="date-input-wrap"><CalendarBlank size={17} /><input type="date" value={filters.dateFrom} onChange={(event) => onChange("dateFrom", event.target.value)} /><b>~</b><input type="date" value={filters.dateTo} onChange={(event) => onChange("dateTo", event.target.value)} /></span>
      </label>
      <SelectField label="文件格式" value={filters.format} options={options.formats || ["全部"]} onChange={(value) => onChange("format", value)} />
      <div className="filter-actions">
        <button className="button button-secondary" type="button" onClick={onReset}><ArrowCounterClockwise size={17} />重置</button>
        <button className="button button-primary" type="submit"><Funnel size={17} />筛选</button>
      </div>
    </form>
  );
}

export function DataTable({
  rows,
  total,
  selectedId,
  onSelect,
  page = 1,
  pageSize = 20,
  onPageChange = () => undefined,
  onPageSizeChange = () => undefined,
  loading = false,
  error = "",
  onRetry = () => undefined,
  compact = false,
}) {
  const pageCount = Math.max(1, Math.ceil(Number(total || 0) / pageSize));
  const safePage = Math.min(page, pageCount);
  const pageTokens = useMemo(() => paginationTokens(safePage, pageCount), [pageCount, safePage]);

  useEffect(() => {
    if (safePage !== page) onPageChange(safePage);
  }, [onPageChange, page, safePage]);

  return (
    <section className={compact ? "table-panel compact" : "table-panel"} aria-label="数据集列表">
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th className="check-column"><Square size={17} aria-label="全选" /></th>
              <th>数据集名称</th>
              <th className="hide-mobile">项目</th>
              <th className="hide-tablet">样品</th>
              <th>测试类型</th>
              <th className="numeric hide-mobile">文件数</th>
              <th>置信度</th>
              <th>状态</th>
              <th className="hide-tablet">最后修改</th>
            </tr>
          </thead>
          <tbody>
            {rows.length ? rows.map((row) => {
              const Icon = modalityIcons[row.modality] || FileText;
              const selected = row.id === selectedId;
              return (
                <tr
                  key={row.id}
                  className={selected ? "is-selected" : ""}
                  tabIndex="0"
                  aria-selected={selected}
                  onClick={() => onSelect(row.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(row.id);
                    }
                  }}
                >
                  <td className="check-column">{selected ? <span className="checked-box"><Check size={12} weight="bold" /></span> : <Square size={17} />}</td>
                  <td><strong className="dataset-name">{row.name}</strong></td>
                  <td className="hide-mobile">{row.project}</td>
                  <td className="hide-tablet">{row.sample}</td>
                  <td><span className="modality"><Icon size={18} />{row.modalityLabel}</span></td>
                  <td className="numeric hide-mobile">{row.fileCount}</td>
                  <td><ConfidenceBar value={row.confidence} /></td>
                  <td><StatusPill code={row.statusCode} label={row.status} /></td>
                  <td className="muted hide-tablet">{row.updatedAt}</td>
                </tr>
              );
            }) : (
              <tr><td colSpan="9"><div className="empty-table"><FileMagnifyingGlass size={30} /><strong>{loading ? "正在加载本地目录" : error ? "无法读取本地目录" : "没有匹配的数据集"}</strong><span>{loading ? "正在等待后端返回当前页。" : error || "调整筛选条件或搜索关键词后再试。"}</span>{error ? <button className="button button-secondary" type="button" onClick={onRetry}>重试</button> : null}</div></td></tr>
            )}
          </tbody>
        </table>
      </div>
      <footer className="table-footer">
        <span>共 {Number(total ?? rows.length).toLocaleString("zh-CN")} 条</span>
        <nav aria-label="数据表分页">
          <button type="button" aria-label="上一页" disabled={safePage === 1 || loading} onClick={() => onPageChange(Math.max(1, safePage - 1))}><CaretLeft size={16} /></button>
          {pageTokens.map((token) => typeof token === "number" ? (
            <button
              className={token === safePage ? "is-current" : ""}
              type="button"
              key={token}
              aria-current={token === safePage ? "page" : undefined}
              aria-label={"第 " + token + " 页"}
              disabled={loading}
              onClick={() => onPageChange(token)}
            >{token}</button>
          ) : <span key={token} aria-hidden="true">…</span>)}
          <button type="button" aria-label="下一页" disabled={safePage === pageCount || loading} onClick={() => onPageChange(Math.min(pageCount, safePage + 1))}><CaretRight size={16} /></button>
          <select
            aria-label="每页条数"
            value={pageSize}
            onChange={(event) => {
              onPageSizeChange(Number(event.target.value));
            }}
          ><option value="20">20 条/页</option><option value="50">50 条/页</option></select>
        </nav>
      </footer>
    </section>
  );
}

function PipelineFooter() {
  const steps = [
    { label: "Inbox 监控", value: "等待新文件", state: "本地目录监控", icon: Tray },
    { label: "自动检测", value: "签名与文件组", state: "按类型自动分组", icon: FileMagnifyingGlass },
    { label: "规则 + 本地模型", value: "生成分类建议", state: "保留可解释证据", icon: Brain },
    { label: "SHA-256", value: "复制前后校验", state: "摘要一致才提交", icon: ShieldCheck },
    { label: "人工确认后入库", value: "受控提交", state: "默认保留源文件", icon: Archive },
  ];
  return (
    <section className="pipeline" aria-labelledby="pipeline-title">
      <h2 id="pipeline-title">入库流程（本地处理）</h2>
      <div className="pipeline-steps">
        {steps.map((step, index) => {
          const Icon = step.icon;
          return (
            <div className="pipeline-step" key={step.label}>
              <span className="pipeline-icon"><Icon size={20} /></span>
              <span><strong>{step.label}</strong><b>{step.value}</b><small>{step.state}</small></span>
              {index < steps.length - 1 ? <i className="pipeline-line" /> : null}
            </div>
          );
        })}
      </div>
    </section>
  );
}

export function DatabasePage({ summary, filters, options, onFilter, onReset, ...tableProps }) {
  return (
    <div className="database-page">
      <SummaryStrip summary={summary} />
      <FiltersBar filters={filters} options={options} onChange={onFilter} onReset={onReset} />
      <DataTable {...tableProps} />
      <PipelineFooter />
    </div>
  );
}
