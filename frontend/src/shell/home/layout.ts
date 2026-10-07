// Home's widget-grid layout model — pure data and pure helpers, no DOM, no
// React. The server stores the whole document (fused_render/shell/home_layout.py
// validates the same vocabulary); array order is grid order.

export type WidgetSource =
  | "apps"
  | "playground"
  | "sessions"
  | "recents"
  | "tasks"
  | "bots"
  | "folder"
  | "index"
  | "app";
export type WidgetSize = "1x1" | "2x1" | "2x2" | "4x1"; // cols x rows
export type WidgetFormat = "cards" | "list" | "icons" | "board" | "count" | "live";

export interface Widget {
  id: string;
  source: WidgetSource;
  size: WidgetSize;
  format: WidgetFormat;
  /** source === "folder": a BookmarkFolder id (platform/lib/bookmarks.ts). */
  folderId?: string;
  /** source === "app": the app's fs path (AppInfo.path). */
  appPath?: string;
}

export interface HomeLayout {
  version: 1;
  widgets: Widget[];
}

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
  apps: {
    label: "Fused Apps",
    description: "Your most recently used apps.",
    sizes: ["4x1", "2x1", "2x2"],
    formats: ["cards", "icons"],
  },
  app: {
    label: "App",
    description: "Run one of your apps right on Home.",
    sizes: ["2x2", "2x1", "4x1"],
    formats: ["live"],
  },
  playground: {
    label: "AI Playground",
    description: "Run AI models on this machine.",
    sizes: ["4x1", "2x1", "2x2"],
    formats: ["cards", "list"],
  },
  sessions: {
    label: "Claude Sessions",
    description: "Folders where you recently used Claude Code.",
    sizes: ["4x1", "2x1", "2x2"],
    formats: ["cards", "list"],
  },
  recents: {
    label: "Recent files",
    description: "Files you opened lately.",
    sizes: ["4x1", "2x1", "2x2"],
    formats: ["cards", "list"],
  },
  tasks: {
    label: "Tasks",
    description: "Open Claude tasks: queued, running, and waiting on you.",
    sizes: ["2x2", "2x1", "4x1"],
    formats: ["list", "board", "count"],
  },
  bots: {
    label: "Bots",
    description: "What your bots are doing right now.",
    sizes: ["1x1", "2x1", "2x2"],
    formats: ["list", "count"],
  },
  folder: {
    label: "Bookmark folder",
    description: "The contents of one of your bookmark folders.",
    sizes: ["2x1", "1x1", "2x2", "4x1"],
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
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize } = {},
): Widget {
  const spec = SOURCES[source];
  const w: Widget = {
    id: opts.id ?? newWidgetId(),
    source,
    size: opts.size && spec.sizes.includes(opts.size) ? opts.size : spec.sizes[0],
    format: opts.format && spec.formats.includes(opts.format) ? opts.format : spec.formats[0],
  };
  if (opts.folderId) w.folderId = opts.folderId;
  if (source === "app" && opts.appPath) w.appPath = opts.appPath;
  return w;
}

export const DEFAULT_LAYOUT: HomeLayout = {
  version: 1,
  widgets: [
    makeWidget("apps", { id: "default-apps" }),
    makeWidget("playground", { id: "default-playground" }),
    makeWidget("sessions", { id: "default-sessions" }),
    makeWidget("recents", { id: "default-recents" }),
  ],
};

/** Fresh copy of the default — callers mutate nothing, but state should never
    alias the module constant. */
export function defaultLayout(): HomeLayout {
  return { version: 1, widgets: DEFAULT_LAYOUT.widgets.map((w) => ({ ...w })) };
}

/** Whatever came off the wire -> a layout the grid can render. Anything that
    is not a version-1 document with a widgets array yields the default;
    an empty array is kept (the user removed everything on purpose). */
export function normalizeLayout(raw: unknown): HomeLayout {
  const r = raw as { version?: unknown; widgets?: unknown } | null;
  if (!r || typeof r !== "object" || r.version !== 1 || !Array.isArray(r.widgets)) {
    return defaultLayout();
  }
  const seen = new Set<string>();
  const widgets: Widget[] = [];
  for (const item of r.widgets.slice(0, MAX_WIDGETS)) {
    if (!item || typeof item !== "object") continue;
    const x = item as Record<string, unknown>;
    const source = x.source as WidgetSource;
    if (!SOURCE_KEYS.includes(source)) continue;
    const folderId = typeof x.folderId === "string" && x.folderId ? x.folderId : undefined;
    if (source === "folder" && !folderId) continue;
    const appPath = typeof x.appPath === "string" && x.appPath ? x.appPath : undefined;
    if (source === "app" && !appPath) continue;
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
    };
    if (source === "folder") w.folderId = folderId;
    if (source === "app") w.appPath = appPath;
    widgets.push(w);
  }
  return { version: 1, widgets };
}

export function moveWidget(layout: HomeLayout, from: number, to: number): HomeLayout {
  const n = layout.widgets.length;
  if (from === to || from < 0 || to < 0 || from >= n || to >= n) return layout;
  const widgets = layout.widgets.slice();
  const [item] = widgets.splice(from, 1);
  widgets.splice(to, 0, item);
  return { ...layout, widgets };
}

export function addWidget(
  layout: HomeLayout,
  source: WidgetSource,
  opts: { id?: string; folderId?: string; appPath?: string; format?: WidgetFormat; size?: WidgetSize } = {},
): HomeLayout {
  if (layout.widgets.length >= MAX_WIDGETS) return layout;
  return { ...layout, widgets: [...layout.widgets, makeWidget(source, opts)] };
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

export function setSize(layout: HomeLayout, id: string, size: WidgetSize): HomeLayout {
  return patch(layout, id, (w) =>
    SOURCES[w.source].sizes.includes(size) && w.size !== size ? { ...w, size } : null,
  );
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
};

/** Menu labels for sizes. */
export const SIZE_LABELS: Record<WidgetSize, string> = {
  "1x1": "Small",
  "2x1": "Half",
  "2x2": "Large",
  "4x1": "Full row",
};
