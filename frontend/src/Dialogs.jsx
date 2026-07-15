import { useEffect, useState } from "react";
import {
  CheckCircle,
  Database,
  FileMagnifyingGlass,
  FolderOpen,
  Plus,
  ShieldCheck,
  SpinnerGap,
  Tray,
  X,
} from "@phosphor-icons/react";

function DialogFrame({ title, description, onClose, children, footer, wide = false }) {
  return (
    <div className="dialog-backdrop" role="presentation" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className={wide ? "dialog wide" : "dialog"} role="dialog" aria-modal="true" aria-labelledby="dialog-title">
        <header><div><h1 id="dialog-title">{title}</h1><p>{description}</p></div><button className="icon-button" type="button" aria-label="关闭对话框" onClick={onClose}><X size={19} /></button></header>
        <div className="dialog-body">{children}</div>
        {footer ? <footer>{footer}</footer> : null}
      </section>
    </div>
  );
}

export function ImportDialog({ onClose, onStartScan }) {
  const [source, setSource] = useState("inbox");
  const [stage, setStage] = useState("ready");
  const [result, setResult] = useState(null);
  async function scan() {
    setStage("scanning");
    try {
      const response = await onStartScan(source);
      setResult(response || {});
      setStage("done");
    } catch {
      setStage("error");
    }
  }
  const footer = stage === "done" ? (
    <><button className="button button-secondary" type="button" onClick={onClose}>稍后处理</button><button className="button button-primary" type="button" onClick={onClose}><CheckCircle size={18} />查看待审核</button></>
  ) : (
    <><button className="button button-secondary" type="button" onClick={onClose}>取消</button><button className="button button-primary" type="button" disabled={stage === "scanning"} onClick={scan}>{stage === "scanning" ? <SpinnerGap className="spin" size={18} /> : <FileMagnifyingGlass size={18} />}{stage === "scanning" ? "正在扫描" : "开始扫描"}</button></>
  );
  return (
    <DialogFrame title="导入数据" description="选择一个本地入口。扫描只建立索引，不会移动、改名或删除源文件。" onClose={onClose} footer={footer}>
      {stage === "done" ? (
        <div className="scan-result"><span><CheckCircle size={36} weight="duotone" /></span><h2>扫描任务已提交</h2><p>任务状态：<strong>{result.status || "QUEUED"}</strong>。处理结果可在“入库记录”中查看。</p><div><ShieldCheck size={17} />所有源文件保持原位，入库前仍需确认。</div></div>
      ) : (
        <>
          <div className="source-options" role="radiogroup" aria-label="扫描来源">
            <label className={source === "inbox" ? "source-option is-selected" : "source-option"}><input autoFocus type="radio" name="source" value="inbox" checked={source === "inbox"} onChange={() => setSource("inbox")} /><Tray size={25} /><span><strong>扫描 Inbox</strong><small>发现新放入的测试数据并进入审核流程</small><b>C:\Research Data\inbox</b></span></label>
            <label className={source === "reference" ? "source-option is-selected" : "source-option"}><input type="radio" name="source" value="reference" checked={source === "reference"} onChange={() => setSource("reference")} /><Database size={25} /><span><strong>扫描参考库</strong><small>只读建立目录索引，不改动任何历史文件</small><b>C:\Research Data\data ref</b></span></label>
          </div>
          <div className="scan-note"><FolderOpen size={17} /><span>系统会识别同名主体和旁车文件，将其组合为一个逻辑数据集。</span></div>
          {stage === "error" ? <p className="form-error">扫描服务暂不可用，请确认后端已启动后重试。</p> : null}
        </>
      )}
    </DialogFrame>
  );
}

export function EditDatasetDialog({ dataset, onClose, onSave, saving = false }) {
  const [draft, setDraft] = useState(dataset);
  useEffect(() => setDraft(dataset), [dataset]);
  if (!draft) return null;
  const set = (field, value) => setDraft((current) => ({ ...current, [field]: value }));
  return (
    <DialogFrame
      title="修改分类"
      description="修改仅更新目录中的分类决策；原始路径和文件内容保持不变。"
      onClose={onClose}
      footer={<><button className="button button-secondary" type="button" disabled={saving} onClick={onClose}>取消</button><button className="button button-primary" type="button" disabled={saving} onClick={() => onSave(draft)}>{saving ? "正在保存" : "保存修改"}</button></>}
    >
      <div className="edit-grid">
        <label className="form-field full"><span>建议规范名称</span><input autoFocus value={draft.canonicalName} onChange={(event) => set("canonicalName", event.target.value)} /></label>
        <label className="form-field"><span>测试类型</span><select value={draft.modality} onChange={(event) => set("modality", event.target.value)}><option value="SEM">SEM</option><option value="TENSILE">拉伸</option><option value="FTIR">FTIR</option><option value="RHEOLOGY">流变</option><option value="IMPACT">冲击</option><option value="GPC">GPC</option><option value="TORQUE">扭矩</option><option value="OPTICAL">光学图像</option><option value="UNKNOWN">未知</option></select></label>
        <label className="form-field"><span>项目</span><input value={draft.project} onChange={(event) => set("project", event.target.value)} /></label>
        <label className="form-field"><span>材料状态</span><select value={draft.materialStateCode} onChange={(event) => set("materialStateCode", event.target.value)}><option value="VIRGIN">干燥态</option><option value="RECYCLED">回收料</option><option value="UNKNOWN">未知</option></select></label>
        <label className="form-field"><span>样品</span><input value={draft.sample} onChange={(event) => set("sample", event.target.value)} /></label>
      </div>
      <div className="immutable-note"><ShieldCheck size={17} /><span><strong>不可变字段</strong>：数据集 UUID、原始路径和 SHA-256 不会被修改。</span></div>
    </DialogFrame>
  );
}

export function NewRuleDialog({ onClose, onSave }) {
  const modalities = [
    ["SEM", "SEM"],
    ["TENSILE", "拉伸（TENSILE）"],
    ["FTIR", "FTIR"],
    ["RHEOLOGY", "流变（RHEOLOGY）"],
    ["TORQUE", "扭矩（TORQUE）"],
    ["IMPACT", "冲击（IMPACT）"],
    ["GPC", "GPC"],
    ["OPTICAL", "光学图像（OPTICAL）"],
    ["SIMULATION", "模拟（SIMULATION）"],
    ["REFERENCE", "参考资料（REFERENCE）"],
    ["UNKNOWN", "未知（UNKNOWN）"],
  ];
  const [draft, setDraft] = useState({ name: "", pattern: "", label: "SEM", priority: 100, enabled: true });
  const set = (field, value) => setDraft((current) => ({ ...current, [field]: value }));
  const valid = draft.name.trim() && draft.pattern.trim() && draft.label;
  return (
    <DialogFrame
      title="新建分类规则"
      description="输入 Python 正则表达式和完整测试类型。规则会在本地模型之前执行，并记录版本和命中证据。"
      onClose={onClose}
      footer={<><button className="button button-secondary" type="button" onClick={onClose}>取消</button><button className="button button-primary" type="button" disabled={!valid} onClick={() => onSave(draft)}><Plus size={17} />创建规则</button></>}
    >
      <div className="edit-grid">
        <label className="form-field full"><span>规则名称</span><input autoFocus value={draft.name} onChange={(event) => set("name", event.target.value)} placeholder="例如：特殊后缀识别为 GPC" /></label>
        <label className="form-field full"><span>Python regex（忽略大小写）</span><textarea rows="3" spellCheck="false" value={draft.pattern} onChange={(event) => set("pattern", event.target.value)} placeholder={"例如：mystery\\.special$"} /><small className="field-help">表达式由后端 Python re 引擎校验，可匹配完整路径与文件名。</small></label>
        <label className="form-field"><span>分类结果（modality label）</span><select value={draft.label} onChange={(event) => set("label", event.target.value)}>{modalities.map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label>
        <label className="form-field"><span>优先级</span><input type="number" min="1" max="999" value={draft.priority} onChange={(event) => set("priority", Number(event.target.value))} /></label>
        <label className="rule-enabled-field full"><input type="checkbox" checked={draft.enabled} onChange={(event) => set("enabled", event.target.checked)} /><span><strong>创建后启用</strong><small>新规则从下一次扫描开始生效。</small></span></label>
      </div>
    </DialogFrame>
  );
}
