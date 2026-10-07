// Home's widget-grid layout model — pure data and pure helpers, no DOM, no
// React. The server stores the whole document (fused_render/shell/home_layout.py
// validates the same vocabulary); every widget owns explicit cells (x, y) and
// array order is reading order (y, x).

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

export interface Widget {
  id: string;
  source: WidgetSource;
  size: WidgetSize;
  format: WidgetFormat;
  /** Column of the top-left cell, 0..3. */
  x: number;
  /** Row of the top-left cell. */
  y: number;
  /** source === "folder": a BookmarkFolder id (platform/lib/bookmarks.ts). */
  folderId?: string;
  /** source === "app": an app folder (AppInfo.path), any fs path the explorer renders, or an http(s) URL. */
  appPath?: string;
}

export interface HomeLayout {
  version: typeof LAYOUT_VERSION;
  widgets: Widget[];
}

/** Current document version. 1 predates the search widget: a version-1 layout
    gets a search widget prepended on load (normalizeLayout). 1 and 2 have no
    coordinates; they are packed densely on load and stamped 3. */
export const LAYOUT_VERSION = 3;

/** Storage coordinate space is always this wide. */
export const GRID_COLS = 4;

/** Sanity bound for y + rows (rows are otherwise unbounded). */
export const MAX_ROWS = 64;

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
    description: "Your most recently used apps.",
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
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize } = {},
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
  return w;
}

export const DEFAULT_LAYOUT: HomeLayout = {
  version: LAYOUT_VERSION,
  widgets: [
    makeWidget("search", 0, 0, { id: "default-search" }),
    makeWidget("apps", 0, 1, { id: "default-apps" }),
    makeWidget("playground", 0, 2, { id: "default-playground" }),
    makeWidget("sessions", 0, 3, { id: "default-sessions" }),
    makeWidget("recents", 0, 4, { id: "default-recents" }),
  ],
};

/** Fresh copy of the default — callers mutate nothing, but state should never
    alias the module constant. */
export function defaultLayout(): HomeLayout {
  return { version: LAYOUT_VERSION, widgets: DEFAULT_LAYOUT.widgets.map((w) => ({ ...w })) };
}

/** Whatever came off the wire -> a layout the grid can render. Anything that
    is not a version-1/2/3 document with a widgets array yields the default;
    an empty array is kept (the user removed everything on purpose). A version-1
    document predates the search widget, so one is prepended. Versions 1 and 2
    carry no coordinates and are packed the way CSS `row dense` placed them;
    version 3 keeps its coordinates, re-placing any that are missing, out of
    bounds or overlapping. The result is stamped current. At most one search
    widget. */
export function normalizeLayout(raw: unknown): HomeLayout {
  const r = raw as { version?: unknown; widgets?: unknown } | null;
  if (
    !r ||
    typeof r !== "object" ||
    (r.version !== 1 && r.version !== 2 && r.version !== LAYOUT_VERSION) ||
    !Array.isArray(r.widgets)
  ) {
    return defaultLayout();
  }
  const seen = new Set<string>();
  const cleaned: { w: Widget; rx: unknown; ry: unknown }[] = [];
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
    cleaned.push({ w, rx: x.x, ry: x.y });
  }
  if (r.version === 1 && !cleaned.some((c) => c.w.source === "search")) {
    cleaned.unshift({ w: makeWidget("search", 0, 0), rx: undefined, ry: undefined });
    if (cleaned.length > MAX_WIDGETS) cleaned.length = MAX_WIDGETS;
  }
  let widgets: Widget[];
  if (r.version === LAYOUT_VERSION) {
    widgets = [];
    for (const { w, rx, ry } of cleaned) {
      const { cols, rows } = dims(w.size);
      let x = rx as number;
      let y = ry as number;
      if (!Number.isInteger(x) || !Number.isInteger(y) || !canPlace(widgets, { x, y, cols, rows })) {
        ({ x, y } = firstFreeSlot(widgets, w.size));
        // Same bound addWidget enforces: a repaired slot past MAX_ROWS drops the widget.
        if (y + rows > MAX_ROWS) continue;
      }
      widgets.push({ ...w, x, y });
    }
  } else {
    widgets = packDense(cleaned.map((c) => c.w));
  }
  return { version: LAYOUT_VERSION, widgets: sortByPosition(widgets) };
}

/** True when the layout already has a search widget (only one is allowed). */
export function hasSearch(layout: HomeLayout): boolean {
  return layout.widgets.some((w) => w.source === "search");
}

export function addWidget(
  layout: HomeLayout,
  source: WidgetSource,
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize } = {},
): HomeLayout {
  if (layout.widgets.length >= MAX_WIDGETS) return layout;
  if (source === "search" && hasSearch(layout)) return layout;
  const probe = makeWidget(source, 0, 0, opts);
  const { x, y } = firstFreeSlot(layout.widgets, probe.size);
  if (y + dims(probe.size).rows > MAX_ROWS) return layout;
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
  if (!w || w.size === size || !SOURCES[w.source].sizes.includes(size)) return layout;
  const { cols, rows } = dims(size);
  const x = Math.min(w.x, GRID_COLS - cols);
  if (!canPlace(layout.widgets, { x, y: w.y, cols, rows }, id)) return layout;
  const widgets = layout.widgets.map((o) => (o.id === id ? { ...o, size, x } : o));
  return { ...layout, widgets: sortByPosition(widgets) };
}

/** Sizes this widget could take right now (feeds the size chips). */
export function allowedSizes(layout: HomeLayout, id: string): WidgetSize[] {
  const w = layout.widgets.find((x) => x.id === id);
  if (!w) return [];
  return SOURCES[w.source].sizes.filter((s) => {
    if (s === w.size) return true;
    const { cols, rows } = dims(s);
    return canPlace(layout.widgets, { x: Math.min(w.x, GRID_COLS - cols), y: w.y, cols, rows }, id);
  });
}

export function setFormat(layout: HomeLayout, id: string, format: WidgetFormat): HomeLayout {
  return patch(layout, id, (w) =>
    SOURCES[w.source].formats.includes(format) && w.format !== format ? { ...w, format } : null,
  );
}

/** Columns and rows a size spans. */
export function dims(size: WidgetSize): { cols: number; rows: number } {
  const [c, r] = size.split("x");
  return { cols: Number(c), rows: Number(r) };
}

/** How many list rows / icon tiles a widget of this size draws before it says
    "+N more". Fixed per size: the grid's row height is fixed, so this needs no
    measuring. A full-row list runs in two columns. */
export function itemCapacity(size: WidgetSize, format: WidgetFormat): number {
  const { cols, rows } = dims(size);
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
  return { x: w.x, y: w.y, ...dims(w.size) };
}

/** Rows the widgets reach down to (max of y + rows); 0 when empty. */
export function rowsUsed(widgets: Widget[]): number {
  let n = 0;
  for (const w of widgets) n = Math.max(n, w.y + dims(w.size).rows);
  return n;
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
export function canPlace(widgets: Widget[], rect: Rect, except?: string, cols = GRID_COLS): boolean {
  if (rect.x < 0 || rect.x + rect.cols > cols || rect.y < 0 || rect.y + rect.rows > MAX_ROWS) return false;
  const occ = occupancy(widgets, except);
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
): { x: number; y: number } {
  const { cols: c, rows } = dims(size);
  return firstFreeRect(widgets.filter((w) => w.id !== except).map(rectOf), c, rows, cols);
}

/** CSS `grid-auto-flow: row dense` on `cols` columns with every item
    auto-placed: each item restarts at (0,0) and takes the first free spot,
    columns left to right then rows top to bottom. Callers clamp widths to
    `cols` first. */
export function packDense<T extends { size: WidgetSize }>(items: T[], cols = GRID_COLS): (T & { x: number; y: number })[] {
  const placed: Rect[] = [];
  return items.map((it) => {
    const { cols: c, rows } = dims(it.size);
    const { x, y } = firstFreeRect(placed, c, rows, cols);
    placed.push({ x, y, cols: c, rows });
    return { ...it, x, y };
  });
}

/** Narrow-screen projection of the 4-column coordinates: reading order, widths
    clamped to `cols`, packed densely. Derived, never stored. */
export function reflowToColumns(layout: HomeLayout, cols: number): Map<string, Rect> {
  const placed: Rect[] = [];
  const out = new Map<string, Rect>();
  for (const w of sortByPosition(layout.widgets)) {
    const d = dims(w.size);
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
  const { cols, rows } = dims(w.size);
  if (!canPlace(layout.widgets, { x, y, cols, rows }, id)) return layout;
  const widgets = layout.widgets.map((o) => (o.id === id ? { ...o, x, y } : o));
  return { ...layout, widgets: sortByPosition(widgets) };
}

/** Alt+Arrow: step one cell, then keep stepping past blocked cells until a
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
  const { cols, rows } = dims(w.size);
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

/** "Tidy up": pack everything densely in reading order. Never automatic. */
export function compactLayout(layout: HomeLayout): HomeLayout {
  return { ...layout, widgets: sortByPosition(packDense(sortByPosition(layout.widgets))) };
}
