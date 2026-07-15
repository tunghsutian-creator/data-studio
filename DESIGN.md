# Accepted UI specification

Concept reference: `design/ui-concept.png`

## Product surface

This is a desktop-first scientific productivity application, not a marketing
site. The primary screen is a table-driven data catalog with a persistent
review inspector.

### App shell

- 208 px left navigation rail.
- 64 px top command bar with global search, offline status and `导入数据`.
- Flexible central table workspace.
- 360-392 px right inspector on wide screens.
- Compact ingestion pipeline footer on the database screen.

### Navigation

Visible labels are exactly:

1. `数据库`
2. `待审核`
3. `入库记录`
4. `分类规则`
5. `设置`

### Main database screen

- restrained summary strip: datasets, review count, local storage, high / medium
  / low-confidence counts;
- filters: project, material state, modality, date and file format;
- compact data table is the dominant component;
- selected row uses a pale-blue full-width highlight;
- confidence is shown by a thin semantic progress bar, not a decorative chart;
- status is short Chinese text with restrained semantic color.

### Inspector

The inspector must show:

- dataset ID and review state;
- grouped files;
- original path;
- suggested canonical name;
- predicted modality, project, material state, sample and date;
- SHA-256 integrity state;
- human-readable rule/model evidence;
- local model score with an explicit warning that it is not certainty;
- actions `接受分类`, `修改`, `暂不处理`.

## Design tokens

```text
background       #F7F9FB
surface          #FFFFFF
surface-subtle   #F4F7F9
text             #172033
text-muted       #667085
border           #DCE3EA
border-strong    #C9D3DD
accent           #0B7285
accent-hover     #075E6D
selection        #E7F3FC
selection-border #9CCAF0
success          #2B9A66
warning          #E99A18
danger           #C94A4A
```

- Fonts: `Inter`, `Noto Sans SC`, `Microsoft YaHei`, system sans-serif.
- UI body: 13-14 px; table rows: 44-46 px; headings: restrained 16-20 px.
- Radius: 6 px controls, 8 px panels; no giant rounded wrappers.
- Shadows: only subtle inspector / dialog elevation.
- Icons: consistent 18-20 px outline icons.
- No gradients, glass effects, bento grids, fake charts, decorative badges,
  illustrations or marketing copy.

## Responsive behavior

- Below 1180 px, inspector becomes a dismissible overlay.
- Below 820 px, navigation collapses to an icon rail and nonessential table
  columns hide.
- Primary actions remain keyboard reachable and have visible focus states.

## Core interaction path

`导入数据` -> scan reference/inbox -> select a dataset -> inspect files and
evidence -> accept / edit / defer -> list and summary update immediately.

