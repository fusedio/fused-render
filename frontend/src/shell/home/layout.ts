// Home's widget-grid layout model — pure data and pure helpers, no DOM, no
// React. The server stores the whole document (fused_render/shell/home_layout.py
// validates the same vocabulary); every widget owns explicit cells (x, y) and
// array order is reading order (y, x). A widget may also carry an explicit
// `cols`/`rows` footprint (an edge-dragged size); without it dims(size) applies.
// The two bare sources (search, build) are the exception: their height is fixed
// in units (FIXED_ROWS) and the size presets only choose their width.

export type WidgetSource =
  | "search"
  | "build"
  | "apps"
  | "playground"
  | "sessions"
  | "recents"
  | "tasks"
  | "bots"
  | "folder"
  | "index"
  | "app";
export type WidgetSize = "1x1" | "2x1" | "1x2" | "2x2" | "4x1"; // cols x rows
export type WidgetFormat = "cards" | "list" | "icons" | "board" | "count" | "live" | "bar";

export type AppsSort = "opened" | "updated" | "name";

export const APPS_SORTS: { value: AppsSort; label: string }[] = [
  { value: "opened", label: "Recently opened" },
  { value: "updated", label: "Recently updated" },
  { value: "name", label: "Name" },
];

function isAppsSort(v: unknown): v is AppsSort {
  return APPS_SORTS.some((s) => s.value === v);
}

export interface Widget {
  id: string;
  source: WidgetSource;
  size: WidgetSize;
  format: WidgetFormat;
  /** Column of the top-left unit, 0..7 (half-cell units). */
  x: number;
  /** Row of the top-left unit (half-cell units). */
  y: number;
  /** Explicit footprint in half-cell units when the user dragged an edge; absent = dims(size). */
  cols?: number;
  rows?: number;
  /** source === "folder": a BookmarkFolder id (platform/lib/bookmarks.ts). */
  folderId?: string;
  /** source === "app": an app folder (AppInfo.path), any fs path the explorer renders, or an http(s) URL. */
  appPath?: string;
  /** source === "apps": card order; absent = "opened". */
  sort?: AppsSort;
}

export interface HomeLayout {
  version: typeof LAYOUT_VERSION;
  widgets: Widget[];
}

/** Current document version. 1 predates the search widget: a version-1 layout
    gets a search widget prepended on load (normalizeLayout). 1 and 2 have no
    coordinates; they are packed densely on load. 3 stores whole-cell
    coordinates and is doubled on load; 4 stores half-cell units; 5 is 4 plus
    the fixed rows of the two bare sources (FIXED_ROWS). */
export const LAYOUT_VERSION = 5;

/** Units per whole cell: the grid's unit is half a cell. */
export const CELL = 2;

/** Storage coordinate space is always this wide (half-cell units: a 1x1 widget is 2 units wide). */
export const GRID_COLS = 8;

/** Sanity bound for y + rows, in units (rows are otherwise unbounded). */
export const MAX_ROWS = 128;

/** Tallest footprint, in units (4 cells). */
export const MAX_WIDGET_ROWS = 8;

/** Same ceiling the server enforces on PUT. */
export const MAX_WIDGETS = 48;

export interface SourceSpec {
  label: string;
  description: string;
  /** First entry is the default. */
  sizes: WidgetSize[];
  formats: WidgetFormat[];
}

export const SOURCES: Record<WidgetSource, SourceSpec> = {
  search: {
    label: "File search",
    description: "Search every file on this machine.",
    sizes: ["4x1", "2x1", "1x1"],
    formats: ["bar"],
  },
  build: {
    label: "Build an app",
    description: "Describe an app and Claude scaffolds it — the prompt box from the Apps page.",
    sizes: ["4x1", "2x1"],
    formats: ["bar"],
  },
  apps: {
    label: "Fused Apps",
    description: "Your apps, by recent use, last edit, or name.",
    sizes: ["4x1", "2x1", "1x2", "2x2"],
    formats: ["cards", "icons"],
  },
  app: {
    label: "Page",
    description: "Show one of your apps, any file the explorer can render, or a website, live on Home.",
    sizes: ["2x2", "2x1", "1x2", "4x1"],
    formats: ["live"],
  },
  playground: {
    label: "AI Playground",
    description: "Run AI models on this machine.",
    sizes: ["4x1", "2x1", "1x2", "2x2"],
    formats: ["cards", "list"],
  },
  sessions: {
    label: "Claude Sessions",
    description: "Folders where you recently used Claude Code.",
    sizes: ["4x1", "2x1", "1x2", "2x2"],
    formats: ["cards", "list"],
  },
  recents: {
    label: "Recent files",
    description: "Files you opened lately.",
    sizes: ["4x1", "2x1", "1x2", "2x2"],
    formats: ["cards", "list"],
  },
  tasks: {
    label: "Tasks",
    description: "Open Claude tasks: queued, running, and waiting on you.",
    sizes: ["2x2", "2x1", "1x2", "4x1"],
    formats: ["list", "board", "count"],
  },
  bots: {
    label: "Bots",
    description: "What your bots are doing right now.",
    sizes: ["1x1", "2x1", "1x2", "2x2"],
    formats: ["list", "count"],
  },
  folder: {
    label: "Bookmark folder",
    description: "The contents of one of your bookmark folders.",
    sizes: ["2x1", "1x2", "1x1", "2x2", "4x1"],
    formats: ["list", "icons"],
  },
  index: {
    label: "File index",
    description: "How many files are searchable, and how fresh.",
    sizes: ["1x1", "2x1"],
    formats: ["count"],
  },
};

const SOURCE_KEYS = Object.keys(SOURCES) as WidgetSource[];

export function newWidgetId(): string {
  const c = globalThis.crypto as Crypto | undefined;
  if (c?.randomUUID) return c.randomUUID().slice(0, 8);
  return Math.random().toString(36).slice(2, 10);
}

function makeWidget(
  source: WidgetSource,
  x: number,
  y: number,
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize; sort?: AppsSort } = {},
): Widget {
  const spec = SOURCES[source];
  const w: Widget = {
    id: opts.id ?? newWidgetId(),
    source,
    size: opts.size && spec.sizes.includes(opts.size) ? opts.size : spec.sizes[0],
    format: opts.format && spec.formats.includes(opts.format) ? opts.format : spec.formats[0],
    x,
    y,
  };
  if (opts.folderId) w.folderId = opts.folderId;
  if (source === "app" && opts.appPath) w.appPath = opts.appPath;
  if (source === "apps" && isAppsSort(opts.sort)) w.sort = opts.sort;
  return w;
}

/** The Legacy preset: the Home page as it was before widgets — search, then
    four full-width one-row strips. */
export const DEFAULT_LAYOUT: HomeLayout = {
  version: LAYOUT_VERSION,
  widgets: [
    makeWidget("search", 0, 0, { id: "default-search" }),
    makeWidget("apps", 0, 1, { id: "default-apps", size: "4x1" }),
    makeWidget("playground", 0, 3, { id: "default-playground", size: "4x1" }),
    makeWidget("sessions", 0, 5, { id: "default-sessions", size: "4x1" }),
    makeWidget("recents", 0, 7, { id: "default-recents", size: "4x1" }),
  ],
};

/** Fresh copy of the default — callers mutate nothing, but state should never
    alias the module constant. */
export function defaultLayout(): HomeLayout {
  return { version: LAYOUT_VERSION, widgets: DEFAULT_LAYOUT.widgets.map((w) => ({ ...w })) };
}

/** Whatever came off the wire -> a layout the grid can render. Anything that
    is not a version-1/2/3/4/5 document with a widgets array yields the default;
    an empty array is kept (the user removed everything on purpose). A version-1
    document predates the search widget, so one is prepended. Versions 1 and 2
    carry no coordinates and are packed the way CSS `row dense` placed them;
    version 3 doubles its cell coordinates into units and versions 4 and 5 keep
    their unit coordinates (and may carry an explicit `cols`/`rows` footprint),
    re-placing any that are missing, out of bounds or overlapping. A fixed-row
    source (search, build) always gets its fixed rows; a version-4 document
    also loses the unit rows that shrink vacated (collapseShrunkRows). The
    result is stamped current. At most one search widget. */
export function normalizeLayout(raw: unknown): HomeLayout {
  const r = raw as { version?: unknown; widgets?: unknown } | null;
  if (
    !r ||
    typeof r !== "object" ||
    (r.version !== 1 && r.version !== 2 && r.version !== 3 && r.version !== 4 && r.version !== LAYOUT_VERSION) ||
    !Array.isArray(r.widgets)
  ) {
    return defaultLayout();
  }
  const seen = new Set<string>();
  const cleaned: { w: Widget; rx: unknown; ry: unknown; rc: unknown; rr: unknown }[] = [];
  for (const item of r.widgets.slice(0, MAX_WIDGETS)) {
    if (!item || typeof item !== "object") continue;
    const x = item as Record<string, unknown>;
    const source = x.source as WidgetSource;
    if (!SOURCE_KEYS.includes(source)) continue;
    const folderId = typeof x.folderId === "string" && x.folderId ? x.folderId : undefined;
    if (source === "folder" && !folderId) continue;
    const appPath = typeof x.appPath === "string" && x.appPath ? x.appPath : undefined;
    if (source === "app" && !appPath) continue;
    if (source === "search" && cleaned.some((c) => c.w.source === "search")) continue;
    const spec = SOURCES[source];
    let id = typeof x.id === "string" && x.id ? x.id : "";
    if (!id || seen.has(id)) id = newWidgetId();
    seen.add(id);
    const w: Widget = {
      id,
      source,
      size: spec.sizes.includes(x.size as WidgetSize) ? (x.size as WidgetSize) : spec.sizes[0],
      format: spec.formats.includes(x.format as WidgetFormat)
        ? (x.format as WidgetFormat)
        : spec.formats[0],
      x: 0,
      y: 0,
    };
    if (source === "folder") w.folderId = folderId;
    if (source === "app") w.appPath = appPath;
    if (source === "apps" && isAppsSort(x.sort)) w.sort = x.sort;
    cleaned.push({ w, rx: x.x, ry: x.y, rc: x.cols, rr: x.rows });
  }
  if (r.version === 1 && !cleaned.some((c) => c.w.source === "search")) {
    cleaned.unshift({ w: makeWidget("search", 0, 0), rx: undefined, ry: undefined, rc: undefined, rr: undefined });
    if (cleaned.length > MAX_WIDGETS) cleaned.length = MAX_WIDGETS;
  }
  let widgets: Widget[];
  // id -> rows a fixed-row widget used to span (version 4 only), for the collapse.
  const oldRows = new Map<string, number>();
  if (r.version === 3 || r.version === 4 || r.version === LAYOUT_VERSION) {
    const k = r.version === 3 ? CELL : 1;
    widgets = [];
    if (r.version === LAYOUT_VERSION) growFixedRows(cleaned);
    for (const { w: w0, rx, ry, rc, rr } of cleaned) {
      let w = w0;
      let x = Number.isInteger(rx) ? (rx as number) * k : NaN;
      let y = Number.isInteger(ry) ? (ry as number) * k : NaN;
      const fixed = FIXED_ROWS[w.source];
      const stored = Number.isInteger(rc) && Number.isInteger(rr);
      if (r.version === 4 && fixed !== undefined) {
        oldRows.set(w.id, stored ? (rr as number) : dims(w.size).rows);
      }
      if ((r.version === 4 || r.version === LAYOUT_VERSION) && stored) {
        const min = minFootprint(w.source);
        const c = rc as number;
        // A fixed-row source takes its rows from FIXED_ROWS, whatever was stored.
        const rw = fixed ?? (rr as number);
        if (
          c >= min.cols && c <= GRID_COLS && rw >= min.rows && rw <= MAX_WIDGET_ROWS &&
          Number.isInteger(x) && x + c <= GRID_COLS
        ) {
          const preset = presetFor(w.source, c, rw);
          w = preset ? { ...w, size: preset } : { ...w, size: contentSizeFor(w.source, c, rw), cols: c, rows: rw };
        }
      }
      const { cols, rows } = dimsOf(w);
      if (!Number.isInteger(x) || !Number.isInteger(y) || !canPlace(widgets, { x, y, cols, rows })) {
        ({ x, y } = firstFreeSlot(widgets, w.size, GRID_COLS, undefined, w.source));
        // Same bound addWidget enforces: a repaired slot past MAX_ROWS drops the widget.
        if (y + dimsOf(w).rows > MAX_ROWS) continue;
      }
      widgets.push({ ...w, x, y });
    }
    if (r.version === 4) {
      // Unit rows a shrunk search/build used to span and nothing covers now.
      const vacated = new Set<number>();
      for (const wd of widgets) {
        const was = oldRows.get(wd.id);
        if (was === undefined) continue;
        for (let y = wd.y + dimsOf(wd).rows; y < wd.y + was; y++) vacated.add(y);
      }
      widgets = collapseShrunkRows(widgets, vacated);
    }
  } else {
    widgets = packDense(cleaned.map((c) => c.w));
  }
  return { version: LAYOUT_VERSION, widgets: sortByPosition(widgets) };
}

/** The mirror image of collapseShrunkRows: a v5 build stored at 3 rows grows
    to 4; everything at or below its old bottom row moves down so nothing
    overlaps. Edits `entries` in place: each entry's `ry` shifts by the rows
    inserted at or above it by OTHER entries, all from the original `ry`s. */
export function growFixedRows(entries: { w: Widget; ry?: unknown; rr?: unknown }[]): void {
  const inserts: { entry: object; row: number; n: number }[] = [];
  for (const e of entries) {
    const fixed = FIXED_ROWS[e.w.source];
    if (fixed === undefined || !Number.isInteger(e.ry) || !Number.isInteger(e.rr)) continue;
    const rr = e.rr as number;
    if (rr < fixed) inserts.push({ entry: e, row: (e.ry as number) + rr, n: fixed - rr });
  }
  if (inserts.length === 0) return;
  const shifts = entries.map((e) =>
    Number.isInteger(e.ry) ? inserts.reduce((s, i) => (i.entry !== e && i.row <= (e.ry as number) ? s + i.n : s), 0) : 0,
  );
  entries.forEach((e, i) => {
    if (shifts[i]) e.ry = (e.ry as number) + shifts[i];
  });
}

/** Delete the `vacated` unit rows no widget covers: every widget below one
    moves up by one, highest row first so the remaining indexes stay valid.
    Rows left empty on purpose are not in `vacated` and stay. */
export function collapseShrunkRows(widgets: Widget[], vacated: Set<number>): Widget[] {
  let out = widgets;
  for (const y of [...vacated].sort((a, b) => b - a)) {
    const covered = out.some((w) => w.y <= y && y < w.y + dimsOf(w).rows);
    if (covered) continue;
    out = out.map((w) => (w.y > y ? { ...w, y: w.y - 1 } : w));
  }
  return out;
}

/** True when the layout already has a search widget (only one is allowed). */
export function hasSearch(layout: HomeLayout): boolean {
  return layout.widgets.some((w) => w.source === "search");
}

export function addWidget(
  layout: HomeLayout,
  source: WidgetSource,
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize; sort?: AppsSort } = {},
): HomeLayout {
  if (layout.widgets.length >= MAX_WIDGETS) return layout;
  if (source === "search" && hasSearch(layout)) return layout;
  const probe = makeWidget(source, 0, 0, opts);
  const { x, y } = firstFreeSlot(layout.widgets, probe.size, GRID_COLS, undefined, source);
  if (y + dimsOf(probe).rows > MAX_ROWS) return layout;
  return { ...layout, widgets: sortByPosition([...layout.widgets, { ...probe, x, y }]) };
}

export function removeWidget(layout: HomeLayout, id: string): HomeLayout {
  if (!layout.widgets.some((w) => w.id === id)) return layout;
  return { ...layout, widgets: layout.widgets.filter((w) => w.id !== id) };
}

function patch(layout: HomeLayout, id: string, fn: (w: Widget) => Widget | null): HomeLayout {
  const idx = layout.widgets.findIndex((w) => w.id === id);
  if (idx < 0) return layout;
  const next = fn(layout.widgets[idx]);
  if (!next) return layout;
  const widgets = layout.widgets.slice();
  widgets[idx] = next;
  return { ...layout, widgets };
}

/** Resize keeps the top-left anchor; x is clamped left only as far as the new
    width needs. Refused (same object) when the new footprint is not free. */
export function setSize(layout: HomeLayout, id: string, size: WidgetSize): HomeLayout {
  const w = layout.widgets.find((x) => x.id === id);
  if (!w || (w.size === size && w.cols === undefined) || !SOURCES[w.source].sizes.includes(size)) return layout;
  const { cols, rows } = dimsFor(w.source, size);
  const x = Math.min(w.x, GRID_COLS - cols);
  if (!canPlace(layout.widgets, { x, y: w.y, cols, rows }, id)) return layout;
  const widgets = layout.widgets.map((o) => {
    if (o.id !== id) return o;
    const { cols: _c, rows: _r, ...rest } = o;
    return { ...rest, size, x };
  });
  return { ...layout, widgets: sortByPosition(widgets) };
}

/** Sizes this widget could take right now (feeds the size chips). */
export function allowedSizes(layout: HomeLayout, id: string): WidgetSize[] {
  const w = layout.widgets.find((x) => x.id === id);
  if (!w) return [];
  const occ = occupancy(layout.widgets, id);
  return SOURCES[w.source].sizes.filter((s) => {
    if (s === w.size) return true;
    const { cols, rows } = dimsFor(w.source, s);
    return canPlace(layout.widgets, { x: Math.min(w.x, GRID_COLS - cols), y: w.y, cols, rows }, id, GRID_COLS, occ);
  });
}

export function setFormat(layout: HomeLayout, id: string, format: WidgetFormat): HomeLayout {
  return patch(layout, id, (w) =>
    SOURCES[w.source].formats.includes(format) && w.format !== format ? { ...w, format } : null,
  );
}

export function setSort(layout: HomeLayout, id: string, sort: AppsSort): HomeLayout {
  return patch(layout, id, (w) =>
    w.source === "apps" && (w.sort ?? "opened") !== sort ? { ...w, sort } : null,
  );
}

/** Columns and rows a size spans, in half-cell units (a 1x1 is 2 x 2). */
export function dims(size: WidgetSize): { cols: number; rows: number } {
  const [c, r] = size.split("x");
  return { cols: Number(c) * CELL, rows: Number(r) * CELL };
}

/** Sources whose height is their content's, in units, whatever the preset or an edge drag says. */
export const FIXED_ROWS: Partial<Record<WidgetSource, number>> = { search: 1, build: 4 };

/** dims() for a source's preset: a fixed-row source keeps its fixed height. */
export function dimsFor(source: WidgetSource, size: WidgetSize): { cols: number; rows: number } {
  const d = dims(size);
  const fixed = FIXED_ROWS[source];
  return fixed === undefined ? d : { cols: d.cols, rows: fixed };
}

/** The widget's footprint in units: its explicit cols/rows, else its preset's dims
    (a fixed-row source always has its fixed rows). */
export function dimsOf(w: Pick<Widget, "source" | "size" | "cols" | "rows">): { cols: number; rows: number } {
  return w.cols !== undefined && w.rows !== undefined
    ? { cols: w.cols, rows: FIXED_ROWS[w.source] ?? w.rows }
    : dimsFor(w.source, w.size);
}

/** Smallest footprint a source allows: its smallest preset on each axis. */
export function minFootprint(source: WidgetSource): { cols: number; rows: number } {
  const ds = SOURCES[source].sizes.map(dims);
  return { cols: Math.min(...ds.map((d) => d.cols)), rows: FIXED_ROWS[source] ?? Math.min(...ds.map((d) => d.rows)) };
}

/** The source's preset whose dims equal (cols, rows) exactly, else null; a
    fixed-row source matches on cols only. */
export function presetFor(source: WidgetSource, cols: number, rows: number): WidgetSize | null {
  const fixed = FIXED_ROWS[source] !== undefined;
  return SOURCES[source].sizes.find((s) => dims(s).cols === cols && (fixed || dims(s).rows === rows)) ?? null;
}

/** The preset that drives a custom footprint's content: the largest (by area,
    ties -> more columns) that fits inside it. */
export function contentSizeFor(source: WidgetSource, cols: number, rows: number): WidgetSize {
  let best: WidgetSize | null = null;
  const fixed = FIXED_ROWS[source] !== undefined;
  for (const s of SOURCES[source].sizes) {
    const d = dims(s);
    if (d.cols > cols || (!fixed && d.rows > rows)) continue;
    if (!best) {
      best = s;
      continue;
    }
    const b = dims(best);
    // A fixed-row source's presets differ in width only: the widest fitting one.
    const a1 = fixed ? d.cols : d.cols * d.rows;
    const a2 = fixed ? b.cols : b.cols * b.rows;
    if (a1 > a2 || (a1 === a2 && d.cols > b.cols)) best = s;
  }
  return best ?? SOURCES[source].sizes[0];
}

/** How many list rows / icon tiles a widget of this size draws before it says
    "+N more". Fixed per size: the grid's row height is fixed, so this needs no
    measuring. A full-row list runs in two columns. */
export function itemCapacity(size: WidgetSize, format: WidgetFormat): number {
  // Only list/icon widgets are counted here, never the fixed-row search/build,
  // so plain dims() (in units; capacity counts whole cells) is right.
  const d = dims(size);
  const cols = d.cols / CELL;
  const rows = d.rows / CELL;
  if (format === "icons") return cols * 3 * rows;
  // 1x2 (cols 1, rows 2) is covered: list 6, icons 6.
  const perColumn = rows === 1 ? 2 : 6;
  return perColumn * (cols >= 4 ? 2 : 1);
}

/** Menu labels for formats. */
export const FORMAT_LABELS: Record<WidgetFormat, string> = {
  cards: "Cards",
  list: "List",
  icons: "Icons",
  board: "Board",
  count: "Count",
  live: "Live",
  bar: "Search bar",
};

/** Menu labels for sizes. */
export const SIZE_LABELS: Record<WidgetSize, string> = {
  "1x1": "Small",
  "2x1": "Half",
  "1x2": "Tall",
  "2x2": "Large",
  "4x1": "Full row",
};

// ---- Placement helpers -----------------------------------------------------

export interface Rect {
  x: number;
  y: number;
  cols: number;
  rows: number;
}

export function rectOf(w: Widget): Rect {
  return { x: w.x, y: w.y, ...dimsOf(w) };
}

/** Rows the widgets reach down to (max of y + rows); 0 when empty. */
export function rowsUsed(widgets: Widget[]): number {
  let n = 0;
  for (const w of widgets) n = Math.max(n, w.y + dimsOf(w).rows);
  return n;
}

/** Rows above the last occupied row that nothing covers: the gaps the user
 *  left, which view mode must keep as tall as the edit canvas does. */
export function emptyRows(rects: { y: number; rows: number }[]): number[] {
  const covered = new Set<number>();
  let end = 0;
  for (const r of rects) {
    for (let j = 0; j < r.rows; j++) covered.add(r.y + j);
    end = Math.max(end, r.y + r.rows);
  }
  const gaps: number[] = [];
  for (let y = 0; y < end; y++) if (!covered.has(y)) gaps.push(y);
  return gaps;
}

/** Reading order (y, x); stable. */
export function sortByPosition(widgets: Widget[]): Widget[] {
  return widgets.slice().sort((a, b) => a.y - b.y || a.x - b.x);
}

function cellsOf(rects: Rect[]): Set<string> {
  const taken = new Set<string>();
  for (const r of rects) {
    for (let j = 0; j < r.rows; j++) for (let i = 0; i < r.cols; i++) taken.add(`${r.x + i},${r.y + j}`);
  }
  return taken;
}

/** "x,y" -> widget id for every occupied cell, optionally ignoring one widget. */
export function occupancy(widgets: Widget[], except?: string): Map<string, string> {
  const m = new Map<string, string>();
  for (const w of widgets) {
    if (w.id === except) continue;
    const r = rectOf(w);
    for (let j = 0; j < r.rows; j++) for (let i = 0; i < r.cols; i++) m.set(`${r.x + i},${r.y + j}`, w.id);
  }
  return m;
}

/** Whole footprint in bounds and every cell free (ignoring `except`'s own). */
export function canPlace(
  widgets: Widget[],
  rect: Rect,
  except?: string,
  cols = GRID_COLS,
  /** Prebuilt `occupancy(widgets, except)`, to reuse across several candidates. */
  occ: Map<string, string> = occupancy(widgets, except),
): boolean {
  if (rect.x < 0 || rect.x + rect.cols > cols || rect.y < 0 || rect.y + rect.rows > MAX_ROWS) return false;
  for (let j = 0; j < rect.rows; j++) {
    for (let i = 0; i < rect.cols; i++) if (occ.has(`${rect.x + i},${rect.y + j}`)) return false;
  }
  return true;
}

/** First free footprint scanning row-major over the rects; a miss opens a new
    row at x = 0. */
export function firstFreeRect(rects: Rect[], cols: number, rows: number, gridCols = GRID_COLS): { x: number; y: number } {
  const taken = cellsOf(rects);
  let used = 0;
  for (const r of rects) used = Math.max(used, r.y + r.rows);
  for (let y = 0; y <= used; y++) {
    for (let x = 0; x + cols <= gridCols; x++) {
      let free = true;
      for (let j = 0; free && j < rows; j++) {
        for (let i = 0; i < cols; i++) {
          if (taken.has(`${x + i},${y + j}`)) {
            free = false;
            break;
          }
        }
      }
      if (free) return { x, y };
    }
  }
  return { x: 0, y: used };
}

export function firstFreeSlot(
  widgets: Widget[],
  size: WidgetSize,
  cols = GRID_COLS,
  except?: string,
  source?: WidgetSource,
): { x: number; y: number } {
  const { cols: c, rows } = source ? dimsFor(source, size) : dims(size);
  return firstFreeRect(widgets.filter((w) => w.id !== except).map(rectOf), c, rows, cols);
}

/** CSS `grid-auto-flow: row dense` on `cols` columns with every item
    auto-placed: each item restarts at (0,0) and takes the first free spot,
    columns left to right then rows top to bottom. Callers clamp widths to
    `cols` first. */
export function packDense<T extends Pick<Widget, "source" | "size" | "cols" | "rows">>(items: T[], cols = GRID_COLS): (T & { x: number; y: number })[] {
  const placed: Rect[] = [];
  return items.map((it) => {
    const { cols: c, rows } = dimsOf(it);
    const { x, y } = firstFreeRect(placed, c, rows, cols);
    placed.push({ x, y, cols: c, rows });
    return { ...it, x, y };
  });
}

/** Narrow-screen projection of the 8-unit coordinates (4 units = 2 cells): reading order, widths
    clamped to `cols`, packed densely. Derived, never stored. */
export function reflowToColumns(layout: HomeLayout, cols: number): Map<string, Rect> {
  const placed: Rect[] = [];
  const out = new Map<string, Rect>();
  for (const w of sortByPosition(layout.widgets)) {
    const d = dimsOf(w);
    const c = Math.min(d.cols, cols);
    const { x, y } = firstFreeRect(placed, c, d.rows, cols);
    const rect = { x, y, cols: c, rows: d.rows };
    placed.push(rect);
    out.set(w.id, rect);
  }
  return out;
}

/** Move to (x, y); the same object when the footprint is not free or in bounds. */
export function placeWidget(layout: HomeLayout, id: string, x: number, y: number): HomeLayout {
  const w = layout.widgets.find((o) => o.id === id);
  if (!w) return layout;
  if (w.x === x && w.y === y) return layout;
  const { cols, rows } = dimsOf(w);
  if (!canPlace(layout.widgets, { x, y, cols, rows }, id)) return layout;
  const widgets = layout.widgets.map((o) => (o.id === id ? { ...o, x, y } : o));
  return { ...layout, widgets: sortByPosition(widgets) };
}

/** Alt+Arrow: step half a cell (one unit), then keep stepping past blocked cells until a
    free slot or the bound. Same object when nothing is possible. */
export function moveByArrow(layout: HomeLayout, id: string, key: string): HomeLayout {
  const w = layout.widgets.find((o) => o.id === id);
  if (!w) return layout;
  const delta: Record<string, [number, number]> = {
    ArrowLeft: [-1, 0],
    ArrowRight: [1, 0],
    ArrowUp: [0, -1],
    ArrowDown: [0, 1],
  };
  const d = delta[key];
  if (!d) return layout;
  const { cols, rows } = dimsOf(w);
  const maxY = rowsUsed(layout.widgets);
  let x = w.x + d[0];
  let y = w.y + d[1];
  while (x >= 0 && x <= GRID_COLS - cols && y >= 0 && y <= maxY) {
    if (canPlace(layout.widgets, { x, y, cols, rows }, id)) return placeWidget(layout, id, x, y);
    x += d[0];
    y += d[1];
  }
  return layout;
}

/** Edge drag: set the footprint in units, top-left anchored. Same object when
    out of bounds, below the source minimum, or not free. */
export function resizeTo(layout: HomeLayout, id: string, cols: number, rows: number): HomeLayout {
  const w = layout.widgets.find((o) => o.id === id);
  if (!w || !Number.isInteger(cols) || !Number.isInteger(rows)) return layout;
  // A fixed-row source cannot change height: any asked-for rows are its own.
  const fixed = FIXED_ROWS[w.source];
  if (fixed !== undefined) rows = fixed;
  const min = minFootprint(w.source);
  if (cols < min.cols || rows < min.rows || w.x + cols > GRID_COLS || rows > MAX_WIDGET_ROWS) return layout;
  if (!canPlace(layout.widgets, { x: w.x, y: w.y, cols, rows }, id)) return layout;
  const preset = presetFor(w.source, cols, rows);
  const { cols: _c, rows: _r, ...rest } = w;
  const next: Widget = preset
    ? { ...rest, size: preset }
    : { ...rest, size: contentSizeFor(w.source, cols, rows), cols, rows };
  if (next.size === w.size && next.cols === w.cols && next.rows === w.rows) return layout;
  const widgets = layout.widgets.map((o) => (o.id === id ? next : o));
  return { ...layout, widgets: sortByPosition(widgets) };
}

/** Alt+Shift+Arrow: grow or shrink the footprint by one unit. */
export function resizeByArrow(layout: HomeLayout, id: string, key: string): HomeLayout {
  const w = layout.widgets.find((o) => o.id === id);
  if (!w) return layout;
  const { cols, rows } = dimsOf(w);
  switch (key) {
    case "ArrowRight":
      return resizeTo(layout, id, cols + 1, rows);
    case "ArrowLeft":
      return resizeTo(layout, id, cols - 1, rows);
    case "ArrowDown":
      return resizeTo(layout, id, cols, rows + 1);
    case "ArrowUp":
      return resizeTo(layout, id, cols, rows - 1);
    default:
      return layout;
  }
}

/** Pack everything densely in reading order. Never automatic. */
export function compactLayout(layout: HomeLayout): HomeLayout {
  return { ...layout, widgets: sortByPosition(packDense(sortByPosition(layout.widgets))) };
}

// ---- Presets and tile swap -------------------------------------------------

export type PresetId = "legacy" | "workbench" | "builder" | "mission" | "files" | "focus";

export const PRESETS: { id: PresetId; name: string; blurb: string }[] = [
  { id: "legacy", name: "Legacy", blurb: "Search, then your apps, playground, sessions and recent files." },
  { id: "workbench", name: "Workbench", blurb: "Search, the build box, and what's running." },
  { id: "builder", name: "Builder", blurb: "A big prompt box with your apps beside it." },
  { id: "mission", name: "Mission control", blurb: "Tasks board first, bots and the index at a glance." },
  { id: "files", name: "Files", blurb: "Search and recent files lead; bookmarks beside them." },
  { id: "focus", name: "Focus", blurb: "Just search and the build box." },
];

type PresetRow = {
  source: WidgetSource;
  x: number;
  y: number;
  size: WidgetSize;
  format?: WidgetFormat;
  /** Explicit footprint in units, for tiles no size preset describes. */
  custom?: { cols: number; rows: number };
  folderId?: string;
};

function presetRows(id: PresetId, folderId?: string): PresetRow[] {
  switch (id) {
    case "legacy":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "apps", x: 0, y: 1, size: "4x1" },
        { source: "playground", x: 0, y: 3, size: "4x1" },
        { source: "sessions", x: 0, y: 5, size: "4x1" },
        { source: "recents", x: 0, y: 7, size: "4x1" },
      ];
    case "workbench":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "build", x: 0, y: 1, size: "4x1" },
        { source: "bots", x: 0, y: 5, size: "1x1" },
        { source: "index", x: 2, y: 5, size: "1x1" },
        { source: "tasks", x: 4, y: 5, size: "2x1" },
        { source: "apps", x: 0, y: 7, size: "4x1" },
        { source: "recents", x: 0, y: 9, size: "4x1" },
      ];
    case "builder":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "build", x: 0, y: 1, size: "2x1" },
        { source: "apps", x: 4, y: 1, size: "2x2", format: "icons" },
        { source: "bots", x: 0, y: 5, size: "2x2", format: "list" },
        { source: "sessions", x: 4, y: 5, size: "2x2", format: "list" },
      ];
    case "mission":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "tasks", x: 0, y: 1, size: "2x2", format: "board", custom: { cols: 6, rows: 4 } },
        { source: "index", x: 6, y: 1, size: "1x1" },
        { source: "bots", x: 6, y: 3, size: "1x1" },
        { source: "sessions", x: 0, y: 5, size: "2x1", format: "list" },
        { source: "recents", x: 4, y: 5, size: "2x1", format: "list" },
      ];
    case "files":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "recents", x: 0, y: 1, size: "2x2", format: "list", custom: { cols: 6, rows: 4 } },
        { source: "index", x: 6, y: 1, size: "1x1" },
        folderId
          ? { source: "folder", x: 6, y: 3, size: "1x1", format: "list", folderId }
          : { source: "bots", x: 6, y: 3, size: "1x1" },
        { source: "apps", x: 0, y: 5, size: "2x1", format: "icons" },
        { source: "tasks", x: 4, y: 5, size: "2x1" },
      ];
    case "focus":
      return [
        { source: "search", x: 0, y: 0, size: "4x1" },
        { source: "build", x: 0, y: 1, size: "4x1" },
      ];
  }
}

/** A preset as a plain layout document with fresh ids. Files shows a bookmark
    folder when it is given one, else bots in that slot. */
export function presetLayout(id: PresetId, opts: { folderId?: string } = {}): HomeLayout {
  const widgets = presetRows(id, opts.folderId).map((r) => {
    const w = makeWidget(r.source, r.x, r.y, { size: r.size, format: r.format, folderId: r.folderId });
    if (r.custom) {
      w.size = contentSizeFor(r.source, r.custom.cols, r.custom.rows);
      w.cols = r.custom.cols;
      w.rows = r.custom.rows;
    }
    return w;
  });
  return { version: LAYOUT_VERSION, widgets: sortByPosition(widgets) };
}

function shapeOf(layout: HomeLayout): string {
  return layout.widgets
    .map((w) => {
      const d = dimsOf(w);
      return `${w.source}|${w.x}|${w.y}|${d.cols}|${d.rows}|${w.format}`;
    })
    .sort()
    .join("\n");
}

/** The preset whose tiles (source, position, footprint, format) this layout has
    exactly; ids, sort, folder and page choices do not matter. Null = custom. */
export function matchPreset(layout: HomeLayout): PresetId | null {
  const mine = shapeOf(layout);
  for (const { id } of PRESETS) {
    if (shapeOf(presetLayout(id, { folderId: "x" })) === mine) return id;
    if (id === "files" && shapeOf(presetLayout(id)) === mine) return id;
  }
  return null;
}

/** Whether `source` can fill `rect` as a tile; `replacingId` is the tile being swapped out. */
export function sourceFits(
  layout: HomeLayout,
  rect: Rect,
  source: WidgetSource,
  replacingId?: string,
): { ok: true } | { ok: false; reason: string } {
  const no = (reason: string) => ({ ok: false as const, reason });
  const min = minFootprint(source);
  if (source === "search") {
    if (layout.widgets.some((w) => w.source === "search" && w.id !== replacingId)) return no("Already on Home");
    return rect.rows === 1 && rect.cols >= min.cols ? { ok: true } : no("Needs a one-row strip");
  }
  if (source === "build") {
    return rect.rows === FIXED_ROWS.build && rect.cols >= 4 ? { ok: true } : no("Needs a bigger tile");
  }
  if (rect.rows === 1) return no("Needs a taller tile");
  // Rows are content-sized, so a source must have a size that fits the rect outright.
  const some = SOURCES[source].sizes.some((sz) => {
    const d = dims(sz);
    return d.cols <= rect.cols && d.rows <= rect.rows;
  });
  if (!some) return no("Needs a bigger tile");
  if (rect.rows > MAX_WIDGET_ROWS) return no("Too tall");
  return { ok: true };
}

/** Where a Change pick lands: an existing tile, or an empty slot. */
export type TileTarget = { kind: "swap"; widget: Widget } | { kind: "fill"; rect: Rect };

export type TileOpts = { folderId?: string; appPath?: string; format?: WidgetFormat; sort?: AppsSort };

/** A widget of `source` covering exactly `rect`: a matching size preset when
    there is one, else an explicit footprint. Null when the source's required
    field (folder / page) is missing. */
/** The format a source opens with in a tile `cols` units wide: card strips
    narrower than a full row show as their compact format (list, or icons for
    apps) — cards need the full width. */
export function defaultFormat(source: WidgetSource, cols: number): WidgetFormat {
  const fs = SOURCES[source].formats;
  return fs[0] === "cards" && cols < GRID_COLS && fs[1] ? fs[1] : fs[0];
}

function tileFor(id: string, source: WidgetSource, rect: Rect, opts: TileOpts, format?: WidgetFormat): Widget | null {
  if (source === "folder" && !opts.folderId) return null;
  if (source === "app" && !opts.appPath) return null;
  const spec = SOURCES[source];
  const f = opts.format ?? format;
  const w: Widget = {
    id,
    source,
    size: spec.sizes[0],
    format: f && spec.formats.includes(f) ? f : defaultFormat(source, rect.cols),
    x: rect.x,
    y: rect.y,
  };
  const preset = presetFor(source, rect.cols, rect.rows);
  if (preset) w.size = preset;
  else {
    w.size = contentSizeFor(source, rect.cols, rect.rows);
    w.cols = rect.cols;
    w.rows = rect.rows;
  }
  if (source === "folder") w.folderId = opts.folderId;
  if (source === "app") w.appPath = opts.appPath;
  if (source === "apps" && isAppsSort(opts.sort)) w.sort = opts.sort;
  return w;
}

/** Show `source` in the tile `id`, keeping its id and rectangle. The same
    object when it does not fit or nothing would change. */
export function swapSource(layout: HomeLayout, id: string, source: WidgetSource, opts: TileOpts = {}): HomeLayout {
  const old = layout.widgets.find((w) => w.id === id);
  if (!old) return layout;
  const rect = rectOf(old);
  if (!sourceFits(layout, rect, source, id).ok) return layout;
  const next = tileFor(id, source, rect, opts, old.source === source ? old.format : undefined);
  if (!next) return layout;
  const keys = new Set([...Object.keys(old), ...Object.keys(next)]) as Set<keyof Widget>;
  if ([...keys].every((k) => old[k] === next[k])) return layout;
  return { ...layout, widgets: layout.widgets.map((w) => (w.id === id ? next : w)) };
}

/** Holes inside the used rows that could hold a tile (at least one cell
    square), as the largest rectangles found scanning in reading order. */
export function emptySlots(layout: HomeLayout): Rect[] {
  const used = rowsUsed(layout.widgets);
  const taken = new Set(occupancy(layout.widgets).keys());
  const free = (x: number, y: number) => !taken.has(`${x},${y}`);
  const out: Rect[] = [];
  for (let y = 0; y < used; y++) {
    for (let x = 0; x < GRID_COLS; x++) {
      if (!free(x, y)) continue;
      let cols = 1;
      while (x + cols < GRID_COLS && free(x + cols, y)) cols++;
      let rows = 1;
      while (y + rows < used && Array.from({ length: cols }, (_, i) => free(x + i, y + rows)).every(Boolean)) rows++;
      for (let j = 0; j < rows; j++) for (let i = 0; i < cols; i++) taken.add(`${x + i},${y + j}`);
      if (cols < CELL || rows < CELL) continue;
      // A hole taller than a tile can hold is offered as stacked slots.
      for (let r = 0; r < rows; r += MAX_WIDGET_ROWS) out.push({ x, y: y + r, cols, rows: Math.min(MAX_WIDGET_ROWS, rows - r) });
    }
  }
  return out;
}

/** Add a tile of `source` covering exactly `rect`. The same object when it does
    not fit, the footprint is not free, or the layout is full. */
export function fillSlot(layout: HomeLayout, rect: Rect, source: WidgetSource, opts: TileOpts = {}): HomeLayout {
  if (layout.widgets.length >= MAX_WIDGETS) return layout;
  if (!sourceFits(layout, rect, source).ok || !canPlace(layout.widgets, rect)) return layout;
  const next = tileFor(newWidgetId(), source, rect, opts);
  if (!next) return layout;
  return { ...layout, widgets: sortByPosition([...layout.widgets, next]) };
}
