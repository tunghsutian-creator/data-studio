import {
  Check,
  CheckCircle,
  Copy,
  FileText,
  FolderOpen,
  Pause,
  PencilSimple,
  Square,
  WarningCircle,
  X,
} from "@phosphor-icons/react";
import { ConfidenceBar, StatusPill } from "./Catalog.jsx";
import { AIAnalysisPanel } from "./AIAnalysisPanel.jsx";

const EMPTY_SELECTION = new Set();

function CopyButton({ value, label, onToast }) {
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      onToast(label + "已复制");
    } catch {
      onToast("浏览器未允许复制，请手动选择文本");
    }
  }
  return <button className="icon-button" type="button" aria-label={"复制" + label} title={"复制" + label} onClick={copy}><Copy size={16} /></button>;
}

function DetailField({ label, value }) {
  return <div className="detail-field"><span>{label}</span><strong>{value || "未识别"}</strong></div>;
}

export function Inspector({
  dataset,
  onClose,
  onAccept,
  onEdit,
  onDefer,
  onToast,
  actionsDisabled = false,
  selectedAssetIds = EMPTY_SELECTION,
  datasetIncludedInExport = false,
  onToggleAssetForExport = () => undefined,
}) {
  if (!dataset) {
    return <div className="inspector-empty"><FileText size={36} /><strong>选择一个数据集</strong><span>查看文件组、分类证据和完整性状态。</span></div>;
  }
  const hasSelectableFiles = Boolean(dataset.files?.length);
  const files = hasSelectableFiles
    ? dataset.files
    : Array.from({ length: dataset.fileCount || 0 }, (_, index) => ({
        id: dataset.id + "-placeholder-" + index,
        name: dataset.name + "_" + (index + 1),
        size: "—",
        role: "关联文件",
      }));
  const score = Math.round(dataset.confidence * 100);
  const warning = score >= 82
    ? "高置信度：仍建议在首次入库前快速核对。"
    : score >= 55
      ? "中置信度：存在部分证据信息不完整，建议人工确认。"
      : "低置信度：分类证据不足，必须人工确认。";

  return (
    <div className="inspector-content">
      <header className="inspector-header">
        <div><strong>{dataset.name}</strong><StatusPill code={dataset.statusCode} label={dataset.status} /></div>
        <button className="icon-button inspector-close" type="button" aria-label="关闭详情" onClick={onClose}><X size={18} /></button>
      </header>

      <div className="inspector-scroll">
        <section className="inspector-section file-group">
          <h2>文件组（{files.length}）</h2>
          <ol>
            {files.map((file, index) => (
              <li className={datasetIncludedInExport || selectedAssetIds.has(file.id) ? "is-export-selected" : ""} key={file.id || file.name + index}>
                <button
                  className="selection-checkbox file-selection-checkbox"
                  type="button"
                  aria-label={datasetIncludedInExport ? `数据集选择已包含 ${file.name}` : selectedAssetIds.has(file.id) ? `取消导出 ${file.name}` : `加入导出 ${file.name}`}
                  aria-pressed={datasetIncludedInExport || selectedAssetIds.has(file.id)}
                  disabled={!hasSelectableFiles || datasetIncludedInExport}
                  onClick={() => onToggleAssetForExport(dataset, file)}
                >
                  {datasetIncludedInExport || selectedAssetIds.has(file.id) ? <span className="checked-box"><Check size={12} weight="bold" /></span> : <Square size={17} />}
                </button>
                <span>{index + 1}<FileText size={15} /></span>
                <strong title={file.role}>{file.name}</strong>
                <small>{file.size}</small>
              </li>
            ))}
          </ol>
        </section>

        <section className="inspector-section">
          <h2>原始路径</h2>
          <div className="copy-field"><FolderOpen size={16} /><span title={dataset.originalPath}>{dataset.originalPath}</span><CopyButton value={dataset.originalPath} label="原始路径" onToast={onToast} /></div>
        </section>

        <section className="inspector-section">
          <h2>建议规范名称</h2>
          <div className="copy-field"><span title={dataset.canonicalName}>{dataset.canonicalName}</span><button className="icon-button" type="button" aria-label="修改建议名称" disabled={actionsDisabled} onClick={onEdit}><PencilSimple size={16} /></button></div>
        </section>

        <section className="inspector-section">
          <div className="detail-grid">
            <DetailField label="测试类型（建议）" value={dataset.modalityLabel} />
            <DetailField label="项目（建议）" value={dataset.project} />
            <DetailField label="材料状态（建议）" value={dataset.materialState} />
            <DetailField label="样品（建议）" value={dataset.sample} />
            <DetailField label="测试日期（建议）" value={dataset.date} />
            <DetailField label="文件格式" value={dataset.format} />
          </div>
        </section>

        <section className="inspector-section">
          <h2>完整性校验（SHA-256）</h2>
          <div className="copy-field hash-field"><span title={dataset.sha256}>{dataset.sha256}</span><CopyButton value={dataset.sha256} label="SHA-256" onToast={onToast} /></div>
          <p className={dataset.hashVerified ? "integrity success" : "integrity warning"}>{dataset.hashVerified ? <CheckCircle size={16} weight="fill" /> : <WarningCircle size={16} weight="fill" />}{dataset.hashVerified ? "校验通过" : "尚未完成校验"}</p>
        </section>

        <section className="inspector-section evidence-section">
          <h2>提取证据</h2>
          <ul>{dataset.evidence.map((item, index) => <li key={item + index}>{item}</li>)}</ul>
        </section>

        <section className="inspector-section model-score">
          <div><h2>规则分类置信度</h2><strong>{score}%</strong></div>
          <ConfidenceBar value={dataset.confidence} showNumber={false} />
          <p><WarningCircle size={15} />{warning} 规则置信度不代表确定事实。</p>
        </section>

        <AIAnalysisPanel datasetId={dataset.id} onToast={onToast} />
      </div>

      <footer className="inspector-actions">
        <button className="button button-primary" type="button" disabled={actionsDisabled} onClick={() => onAccept(dataset.id)}><Check size={17} weight="bold" />接受分类</button>
        <button className="button button-secondary" type="button" disabled={actionsDisabled} onClick={onEdit}><PencilSimple size={17} />修改</button>
        <button className="button button-secondary" type="button" disabled={actionsDisabled} onClick={() => onDefer(dataset.id)}><Pause size={17} />暂不处理</button>
      </footer>
    </div>
  );
}
