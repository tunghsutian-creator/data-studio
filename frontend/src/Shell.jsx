import {
  Atom,
  CheckSquare,
  ClockCounterClockwise,
  Database,
  GearSix,
  ListChecks,
  MagnifyingGlass,
  UploadSimple,
  UserCircle,
} from "@phosphor-icons/react";

const navigation = [
  { id: "database", label: "数据库", icon: Database },
  { id: "review", label: "待审核", icon: CheckSquare },
  { id: "ingest", label: "入库记录", icon: ClockCounterClockwise },
  { id: "rules", label: "分类规则", icon: ListChecks },
  { id: "settings", label: "设置", icon: GearSix },
];

function Sidebar({ activePage, onNavigate }) {
  return (
    <aside className="sidebar" aria-label="主导航">
      <div className="brand" aria-label="PAW 个人学术工作站">
        <span className="brand-mark"><Atom size={31} weight="regular" /></span>
        <span className="brand-copy">
          <strong>PAW</strong>
          <span>Personal Academic<br />AI Workstation</span>
        </span>
      </div>

      <nav className="nav-list">
        {navigation.map((item) => {
          const Icon = item.icon;
          return (
            <button
              className={activePage === item.id ? "nav-item is-active" : "nav-item"}
              key={item.id}
              type="button"
              aria-current={activePage === item.id ? "page" : undefined}
              title={item.label}
              onClick={() => onNavigate(item.id)}
            >
              <Icon size={20} weight={activePage === item.id ? "duotone" : "regular"} />
              <span>{item.label}</span>
            </button>
          );
        })}
      </nav>

      <div className="sidebar-footer">
        <div className="local-state"><span className="status-dot" />本地模式</div>
        <span className="version">v1.3.0</span>
      </div>
    </aside>
  );
}

function Topbar({ query, onQuery, searchRef, source, partialFallback, onImport }) {
  const statusLabel = source === "api" ? "数据库已就绪" : source === "loading" ? "正在连接数据库" : "演示数据已就绪";
  return (
    <header className="topbar">
      <label className="global-search">
        <MagnifyingGlass size={19} aria-hidden="true" />
        <span className="sr-only">全局搜索</span>
        <input
          ref={searchRef}
          type="search"
          value={query}
          onChange={(event) => onQuery(event.target.value)}
          placeholder="全局搜索（项目 / 样品 / 文件名 / 关键字 / SHA-256）"
        />
        <kbd>Ctrl K</kbd>
      </label>

      <div className="topbar-actions">
        <div className="database-state" title={partialFallback ? "部分数据使用本地回退" : "所有数据来自本地数据库"}>
          <UserCircle size={22} />
          <span><strong>本地模式</strong><small><i className={source === "seed" ? "status-dot warning" : "status-dot"} />{statusLabel}</small></span>
        </div>
        <button className="button button-primary import-button" type="button" aria-label="导入数据" onClick={onImport}>
          <UploadSimple size={19} weight="bold" />
          <span>导入数据</span>
        </button>
      </div>
    </header>
  );
}

export function AppShell({
  activePage,
  onNavigate,
  query,
  onQuery,
  searchRef,
  source,
  partialFallback,
  onImport,
  inspector,
  inspectorOpen,
  onCloseInspector,
  toast,
  children,
}) {
  return (
    <div className={inspector ? "app-shell has-inspector" : "app-shell"}>
      <a className="skip-link" href="#workspace">跳到主要内容</a>
      <Sidebar activePage={activePage} onNavigate={onNavigate} />
      <Topbar
        query={query}
        onQuery={onQuery}
        searchRef={searchRef}
        source={source}
        partialFallback={partialFallback}
        onImport={onImport}
      />
      <main id="workspace" className="workspace" tabIndex="-1">{children}</main>
      {inspector ? (
        <>
          <button
            className={inspectorOpen ? "inspector-scrim is-visible" : "inspector-scrim"}
            type="button"
            tabIndex={inspectorOpen ? 0 : -1}
            aria-label="关闭审核详情"
            onClick={onCloseInspector}
          />
          <aside className={inspectorOpen ? "inspector is-open" : "inspector"} aria-label="数据集审核详情">
            {inspector}
          </aside>
        </>
      ) : null}
      <div className={toast ? "toast is-visible" : "toast"} role="status" aria-live="polite">{toast}</div>
    </div>
  );
}
