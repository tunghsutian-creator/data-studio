import { useEffect, useState } from "react";
import {
  Check,
  ClockCounterClockwise,
  Database,
  FileMagnifyingGlass,
  FolderOpen,
  Info,
  LockKey,
  Plus,
  ShieldCheck,
  SlidersHorizontal,
  WarningCircle,
} from "@phosphor-icons/react";
import { DataTable, StatusPill } from "./Catalog.jsx";

function PageHeader({ eyebrow, title, description, action }) {
  return (
    <header className="page-header">
      <div><span>{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>
      {action}
    </header>
  );
}

export function ReviewPage({ rows, total, ...tableProps }) {
  return (
    <div className="secondary-page review-page">
      <PageHeader eyebrow="人工决策队列" title="待审核" description="模型只提供可解释建议；模糊分类必须由你确认后才能进入受管目录。" />
      <section className="review-overview">
        <div><WarningCircle size={22} /><span><strong>{Number(total || 0).toLocaleString("zh-CN")}</strong> 条待处理</span></div>
        <p>建议优先处理低置信度和存在文件组冲突的数据集。</p>
      </section>
      <DataTable rows={rows} total={total} {...tableProps} compact />
    </div>
  );
}

export function IngestPage({ jobs, onImport }) {
  return (
    <div className="secondary-page">
      <PageHeader
        eyebrow="可追溯操作日志"
        title="入库记录"
        description="查看扫描、校验和受管复制的完整过程。源文件默认保留，失败操作不会覆盖原始数据。"
        action={<button className="button button-primary" type="button" onClick={onImport}><Plus size={18} />新建扫描</button>}
      />
      <section className="safety-banner"><ShieldCheck size={24} /><div><strong>安全边界已启用</strong><span>所有受管复制均经过 SHA-256 校验；参考库仅建立索引，绝不移动或改名。</span></div></section>
      <section className="records-panel">
        <div className="records-head"><span>任务</span><span>来源</span><span>处理结果</span><span>状态</span><span>开始时间</span></div>
        {jobs.map((job) => (
          <article className="record-row" key={job.id}>
            <div><span className="record-icon"><ClockCounterClockwise size={19} /></span><span><strong>{job.id}</strong><small>{job.note}</small></span></div>
            <span>{job.source}</span>
            <span className="record-counts"><b>{job.committed}/{job.detected}</b><small>已入库 / 检出</small></span>
            <StatusPill code={job.statusCode === "complete" ? "ingested" : "review"} label={job.status} />
            <span className="muted">{job.startedAt}</span>
          </article>
        ))}
      </section>
    </div>
  );
}

export function RulesPage({ rules, onToggle, onAdd }) {
  const [search, setSearch] = useState("");
  const visible = rules.filter((rule) => [rule.name, rule.description, rule.scope, rule.pattern, rule.label]
    .map((value) => String(value || ""))
    .join(" ")
    .toLowerCase()
    .includes(search.toLowerCase()));
  return (
    <div className="secondary-page">
      <PageHeader
        eyebrow="可解释分类引擎"
        title="分类规则"
        description="规则优先于本地模型，并保留版本和命中证据。停用规则不会删除历史决策。"
        action={<button className="button button-primary" type="button" onClick={onAdd}><Plus size={18} />新建规则</button>}
      />
      <div className="rules-toolbar"><label><FileMagnifyingGlass size={18} /><input type="search" placeholder="搜索规则名称、范围或说明" value={search} onChange={(event) => setSearch(event.target.value)} /></label><span>{visible.length} 条规则</span></div>
      <section className="rules-list">
        {visible.map((rule) => (
          <article className={rule.enabled ? "rule-card" : "rule-card is-disabled"} key={rule.id}>
            <div className="rule-priority"><span>{rule.priority}</span><small>优先级</small></div>
            <div className="rule-copy"><div><h2>{rule.name}</h2><span className="rule-scope">{rule.scope}</span><span className="rule-version">{rule.version}</span><span className={rule.source === "user" ? "rule-source user" : "rule-source"}>{rule.source === "user" ? "用户" : "内置"}</span></div><p>{rule.description}</p><small>{rule.matches === null || rule.matches === undefined ? (rule.source === "user" ? "下一次扫描起生效" : "随扫描实时评估") : "已匹配 " + Number(rule.matches).toLocaleString("zh-CN") + " 个数据集"}</small></div>
            <label className={rule.source === "user" ? "switch" : "switch is-locked"} title={rule.source === "user" ? "启用或停用用户规则" : "内置规则由系统维护，不可停用"}><input type="checkbox" checked={rule.enabled} disabled={rule.source !== "user"} onChange={() => onToggle(rule.id)} /><span aria-hidden="true" /><b>{rule.source !== "user" ? "内置" : rule.enabled ? "已启用" : "已停用"}</b></label>
          </article>
        ))}
      </section>
    </div>
  );
}

function SettingsSection({ icon: Icon, title, description, children }) {
  return <section className="settings-section"><header><Icon size={21} /><div><h2>{title}</h2><p>{description}</p></div></header><div className="settings-body">{children}</div></section>;
}

function ToggleRow({ label, description, checked, onChange }) {
  return <label className="toggle-row"><span><strong>{label}</strong><small>{description}</small></span><span className="switch"><input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} /><i aria-hidden="true" /></span></label>;
}

export function SettingsPage({ config, onSave, saving = false }) {
  const [draft, setDraft] = useState(config);
  useEffect(() => setDraft(config), [config]);
  const update = (field, value) => setDraft((current) => ({ ...current, [field]: value }));
  return (
    <div className="secondary-page settings-page">
      <PageHeader
        eyebrow="本地工作站配置"
        title="设置"
        description="所有路径、规则和模型均保存在本机；系统仅绑定 127.0.0.1。"
        action={<button className="button button-primary" type="button" disabled={saving} onClick={() => onSave(draft)}><Check size={18} />{saving ? "正在保存" : "保存设置"}</button>}
      />
      <div className="settings-grid">
        <SettingsSection icon={FolderOpen} title="本地路径" description="参考库只读，Inbox 是新数据入口，Vault 保存批准后的受管副本。">
          {[['referencePath','参考库'],['inboxPath','Inbox'],['vaultPath','Vault'],['catalogPath','目录数据库'],['exportPath','导出目录'],['backupPath','备份目录']].map(([field, label]) => <label className="settings-field" key={field}><span>{label}</span><input value={draft[field] || ""} onChange={(event) => update(field, event.target.value)} /></label>)}
        </SettingsSection>
        <SettingsSection icon={ShieldCheck} title="安全与校验" description="这些保护项默认开启，避免原始科研数据被覆盖或误删。">
          <ToggleRow label="保留源文件" description="入库后不移动或删除 Inbox 中的原文件。" checked={Boolean(draft.retainSource)} onChange={(value) => update('retainSource', value)} />
          <ToggleRow label="SHA-256 完整性校验" description="受管复制提交前必须与源文件摘要一致。" checked={Boolean(draft.verifySha256)} onChange={(value) => update('verifySha256', value)} />
        </SettingsSection>
        <SettingsSection icon={Database} title="自动扫描" description="仅扫描新增或已变化的文件，不读取未支持格式的正文。">
          <ToggleRow label="监控 Inbox" description="定期扫描新数据并建立待审核记录。" checked={Boolean(draft.autoScan)} onChange={(value) => update('autoScan', value)} />
          <label className="settings-field"><span>扫描间隔</span><select value={draft.scanInterval || '15 分钟'} onChange={(event) => update('scanInterval', event.target.value)}><option>5 分钟</option><option>15 分钟</option><option>30 分钟</option><option>手动</option></select></label>
        </SettingsSection>
        <SettingsSection icon={SlidersHorizontal} title="本地分类模型" description="模型仅辅助排序，不拥有移动、覆盖或删除文件的权限。">
          <label className="settings-field"><span>模型</span><select value={draft.model || 'local-lightweight-v1'} onChange={(event) => update('model', event.target.value)}><option value="local-lightweight-v1">本地轻量分类器 v1</option><option value="rules-only">仅使用规则</option></select></label>
          <p className="settings-note"><Info size={16} />当前为固定人工审核策略：扫描和模型只生成建议，任何置信度都不会自动接受或写入受管 Vault。</p>
        </SettingsSection>
      </div>
      <footer className="settings-security"><LockKey size={17} />当前服务仅监听本机地址，未配置任何云端 API。</footer>
    </div>
  );
}
