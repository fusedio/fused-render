// The "Add a widget" sheet: a gallery of widget previews drawn at their real
// proportions, one per source and format (gallery.ts), grouped by source.
// Clicking one adds it at its default size; sizing lives on the tile's Change
// card. A folder or page first asks which one. Opened from that popover
// (`target`) it lists the one source for that tile and a pick swaps or fills it.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { contractHome, useHome } from "../../apps/explorer/listing/home-path";
import { listDir, statPath, type FsEntry } from "@platform/lib/api";
import { appFolderLine, filterPickerApps, normalizeWebUrl, pickerApps } from "./appPicker";
import {
  AppWindow,
  Bookmark,
  Bot,
  Clock,
  Database,
  FileText,
  Folder,
  FolderGit2,
  Hammer,
  LayoutGrid,
  ListChecks,
  Search,
  Sparkles,
  X,
  type LucideIcon,
} from "lucide-react";
import { createPortal } from "react-dom";
import { ArrowLeft } from "lucide-react";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { isTopmost, popModal, pushModal } from "@platform/ui/modal/esc-stack";
import { isFolder, loadBookmarks, type BookmarkFolder, type BookmarkItem } from "@platform/lib/bookmarks";
import { GRID_COLS, MAX_WIDGETS, SOURCES, type TileTarget, type Widget, type WidgetSource } from "./layout";
import { freeSpaceNote, galleryEntries, previewPx, previewScale, type GalleryEntry } from "./gallery";
import { WidgetBody } from "./Widget";
import type { HomeLayoutApi } from "./useHomeLayout";
import { AppGlyph } from "./widgets/AppsWidget";
import { appName, useAllApps } from "./widgets/AppEmbedWidget";

const SOURCE_ICONS: Record<WidgetSource, LucideIcon> = {
  search: Search,
  build: Hammer,
  apps: LayoutGrid,
  app: AppWindow,
  playground: Sparkles,
  sessions: FolderGit2,
  recents: Clock,
  tasks: ListChecks,
  bots: Bot,
  folder: Bookmark,
  index: Database,
};

/** The source's mark: a lucide glyph on an accent-tinted rounded square. */
export function SourceIcon({ source, large }: { source: WidgetSource; large?: boolean }) {
  const Icon = SOURCE_ICONS[source];
  return (
    <span className={"hw-src-sq" + (large ? " is-lg" : "")} aria-hidden="true">
      <Icon size={large ? 22 : 16} />
    </span>
  );
}

export function allFolders(items: BookmarkItem[], out: BookmarkFolder[] = []): BookmarkFolder[] {
  for (const it of items) {
    if (isFolder(it)) {
      out.push(it);
      allFolders(it.children, out);
    }
  }
  return out;
}

const joinPath = (dir: string, name: string) => dir.replace(/[\\/]+$/, "") + "/" + name;
const parentDir = (dir: string) => dir.replace(/[\\/]+$/, "").replace(/[^\\/]*$/, "").replace(/(.)[\\/]+$/, "$1") || "/";


export function AddWidgetPanel({
  api,
  onClose,
  target,
  initialSource,
}: {
  api: HomeLayoutApi;
  onClose: () => void;
  /** Set when opened from a tile's Change popover. */
  target?: TileTarget;
  initialSource?: WidgetSource;
}) {
  useBookmarksVersion();
  const dialog = useRef<HTMLDivElement>(null);
  // A folder or page entry waits here for its follow-up picker.
  const [pending, setPending] = useState<GalleryEntry | null>(null);
  const entries = useMemo(
    () => galleryEntries(api.layout, target, target ? (initialSource ?? "folder") : undefined),
    [api.layout, target, initialSource],
  );
  const sections = useMemo(() => {
    const out: { source: WidgetSource; entries: GalleryEntry[] }[] = [];
    for (const e of entries) {
      const last = out[out.length - 1];
      if (last && last.source === e.source) last.entries.push(e);
      else out.push({ source: e.source, entries: [e] });
    }
    return out;
  }, [entries]);
  // Previews are drawn at the live tile size, then scaled by one shared factor.
  const [metrics, setMetrics] = useState<PreviewMetrics | null>(null);
  useLayoutEffect(() => {
    if (pending) return;
    const body = dialog.current?.querySelector<HTMLElement>(".hw-gal-body");
    if (body) setMetrics(measurePreviews(body));
  }, [pending]);
  const full = (!target || target.kind === "fill") && api.layout.widgets.length >= MAX_WIDGETS;
  const note = full
    ? `Home is full (${MAX_WIDGETS} widgets)`
    : target
      ? "Pick what this tile shows"
      : freeSpaceNote(api.layout.widgets);

  const source = pending?.source ?? "folder";
  const appsState = useAllApps(source === "app" && !!pending);
  const home = useHome();
  const [appQuery, setAppQuery] = useState("");
  const rawApps = appsState.apps;
  const allApps = useMemo(() => (rawApps ? pickerApps(rawApps) : null), [rawApps]);
  const shownApps = filterPickerApps(allApps ?? [], appQuery, home);
  const [pageMode, setPageMode] = useState<"apps" | "file" | "url">("apps");
  const [urlInput, setUrlInput] = useState("");
  const [filePath, setFilePath] = useState<string>("");
  const [pathInput, setPathInput] = useState("");
  const [browseDir, setBrowseDir] = useState<string | null>(null);
  const [fileCheck, setFileCheck] = useState<{ path: string; ok: boolean; reason?: string; mode?: string } | null>(null);
  const dirCache = useRef(new Map<string, FsEntry[]>());
  const [dirEntries, setDirEntries] = useState<FsEntry[] | null>(null);
  const [dirError, setDirError] = useState(false);
  const dir = browseDir ?? home ?? null;
  const fileMode = source === "app" && pageMode === "file";
  const urlMode = source === "app" && pageMode === "url";
  const url = normalizeWebUrl(urlInput);
  const noApps = source === "app" && pageMode === "apps" && allApps !== null && !allApps.length;
  useEffect(() => {
    if (!fileMode || !dir) return;
    const hit = dirCache.current.get(dir);
    if (hit) {
      setDirEntries(hit);
      setDirError(false);
      return;
    }
    setDirEntries(null);
    setDirError(false);
    let alive = true;
    listDir(dir).then(
      (r) => {
        dirCache.current.set(dir, r.entries);
        if (alive) setDirEntries(r.entries);
      },
      () => alive && setDirError(true),
    );
    return () => {
      alive = false;
    };
  }, [fileMode, dir]);
  useEffect(() => {
    if (!filePath) return;
    let alive = true;
    setFileCheck(null);
    statPath(filePath).then(
      (r) => alive && setFileCheck({ path: filePath, ok: r.templates.length > 0, mode: r.templates[0]?.mode }),
      () => alive && setFileCheck({ path: filePath, ok: false, reason: "Can't read that path." }),
    );
    return () => {
      alive = false;
    };
  }, [filePath]);
  const fileOk = !!fileCheck?.ok && fileCheck.path === filePath;
  const commitPath = async () => {
    let p = pathInput.trim();
    if (!p) return;
    if (home && (p === "~" || p.startsWith("~/"))) p = home + p.slice(1);
    try {
      const r = await statPath(p);
      if (r.is_dir) {
        setBrowseDir(p);
        // A directory that itself renders (a .zarr store) can also be chosen.
        if (r.templates.length) setFilePath(p);
      } else setFilePath(p);
    } catch {
      setFilePath(p);
    }
  };
  const shownEntries = useMemo(
    () =>
      (dirEntries ?? [])
        .filter((e) => !e.name.startsWith(".") && !e.ignored)
        .sort((a, b) => Number(b.is_dir) - Number(a.is_dir) || a.name.localeCompare(b.name)),
    [dirEntries],
  );
  const folders = allFolders(loadBookmarks());

  // Focus the dialog on open; hand focus back to whatever opened it on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => opener?.focus?.();
  }, []);

  // Join the shared modal Esc stack so a modal opened over this sheet gets the
  // press first instead of both closing at once.
  const escToken = useRef({});
  useEffect(() => {
    const token = escToken.current;
    pushModal(token);
    return () => popModal(token);
  }, []);
  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape" || !isTopmost(escToken.current)) return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [onClose]);

  // Keep Tab inside the dialog.
  const onDialogKey = (e: KeyboardEvent) => {
    if (e.key !== "Tab" || !dialog.current) return;
    const items = Array.from(
      dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), [tabindex]:not([tabindex='-1'])"),
    ).filter((el) => el !== dialog.current);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === dialog.current)) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && active === last) {
      e.preventDefault();
      first.focus();
    }
  };

  // Add (or swap in) the entry at its default size, then close. The tile keeps
  // its footprint in target mode, so no size is passed there.
  const commit = (e: GalleryEntry, extra: { folderId?: string; appPath?: string } = {}) => {
    if (e.disabled) return;
    const opts = { format: e.format, ...extra };
    if (target) {
      if (target.kind === "swap") api.swap(target.widget.id, e.source, opts);
      else api.fill(target.rect, e.source, opts);
      onClose();
      return;
    }
    api.add(e.source, { ...opts, size: e.size });
    onClose();
    // The new widget is the last one in the grid; wait a beat for it to mount.
    setTimeout(() => {
      const all = document.querySelectorAll(".hw-widget");
      all[all.length - 1]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 80);
  };
  const choose = (e: GalleryEntry) => {
    if (e.disabled) return;
    if (e.source === "folder" || e.source === "app") setPending(e);
    else commit(e);
  };
  const addCustom = () => {
    if (!pending) return;
    if (urlMode ? url : fileMode && fileOk) commit(pending, { appPath: urlMode ? url! : filePath });
  };
  const needsConfirm = urlMode || fileMode;

  return createPortal(
    <div className="hw-scrim" onPointerDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={dialog} className="hw-sheet" role="dialog" aria-modal="true" aria-label="Add a widget" tabIndex={-1} onKeyDown={onDialogKey}>
        <div className="hw-gal-head">
          {pending ? (
            <button type="button" className="hw-gal-back" onClick={() => setPending(null)}>
              <ArrowLeft size={14} aria-hidden="true" />
              Back
            </button>
          ) : null}
          <h3 className="hw-sheet-title">{pending ? (pending.source === "folder" ? "Pick a bookmark folder" : "Pick a page") : target ? "Put in this tile" : "Add a widget"}</h3>
          <span className="hw-gal-note">{pending ? null : note}</span>
          {pending && needsConfirm ? (
            <button type="button" className="hw-tb is-primary hw-gal-add" disabled={urlMode ? !url : !fileOk} onClick={addCustom}>
              {target ? "Put in this tile" : "Add"}
            </button>
          ) : null}
          <button type="button" className="hw-gal-x" aria-label="Close" onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
        </div>
        <div className="hw-gal-body">
          {pending ? (
            pending.source === "folder" ? (
              <div className="hw-stage-box is-folders">
                {folders.length ? (
                  <div className="hw-folders" role="radiogroup" aria-label="Bookmark folder">
                    {folders.map((f) => (
                      <button key={f.id} type="button" className="hw-folder" onClick={() => commit(pending, { folderId: f.id })}>
                        {f.name}
                      </button>
                    ))}
                  </div>
                ) : (
                  <div className="hw-nofolders">
                    <SourceIcon source="folder" large />
                    <b>No bookmark folders yet</b>
                    <span>Bookmark a folder from the file explorer, then come back to pin it here.</span>
                  </div>
                )}
              </div>
            ) : (
              <div className="hw-stage-box is-folders">
                <div className="hw-chips" role="radiogroup" aria-label="Pick from">
                  {(["apps", "file", "url"] as const).map((m) => (
                    <button
                      key={m}
                      type="button"
                      role="radio"
                      aria-checked={m === pageMode}
                      className={"hw-sizechip" + (m === pageMode ? " is-on" : "")}
                      onClick={() => setPageMode(m)}
                    >
                      {m === "apps" ? "Apps" : m === "file" ? "Any file" : "Website"}
                    </button>
                  ))}
                </div>
                {urlMode ? (
                  <>
                    <input
                      className="hw-appsearch"
                      type="url"
                      placeholder="https://example.com"
                      aria-label="Website URL"
                      value={urlInput}
                      onChange={(e) => setUrlInput(e.target.value)}
                    />
                    {urlInput.trim() ? (
                      <div className={"hw-filepick-status " + (url ? "is-ok" : "is-err")}>
                        {url
                          ? `Will show ${new URL(url).hostname}. Sites that block framing stay blank.`
                          : "Enter a full address, like https://example.com"}
                      </div>
                    ) : null}
                  </>
                ) : fileMode ? (
                  <>
                    <input
                      className="hw-appsearch"
                      type="text"
                      placeholder="Path, e.g. ~/notes/todo.md"
                      aria-label="File path"
                      value={pathInput}
                      onChange={(e) => setPathInput(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          void commitPath();
                        }
                      }}
                      onBlur={() => void commitPath()}
                    />
                    <div className="hw-filepick">
                      {dir ? (
                        <div className="hw-filepick-crumb">
                          {dir !== "/" ? (
                            <button type="button" className="hw-filepick-up" aria-label="Up one folder" onClick={() => setBrowseDir(parentDir(dir))}>
                              ..
                            </button>
                          ) : null}
                          <span>{contractHome(dir, home)}</span>
                        </div>
                      ) : null}
                      <div className="hw-filepick-list" role="radiogroup" aria-label="File">
                        {dirError ? <span className="hw-empty">Can't read that folder.</span> : null}
                        {!dirError && dirEntries === null ? (
                          <span className="hw-app-skel skel-bar" role="status" aria-busy="true" aria-label="Loading folder" />
                        ) : null}
                        {shownEntries.slice(0, 200).map((e) => {
                          const p = joinPath(dir ?? "", e.name);
                          const on = !e.is_dir && p === filePath;
                          return (
                            <button
                              key={e.name}
                              type="button"
                              role="radio"
                              aria-checked={on}
                              className={"hw-appopt" + (on ? " is-on" : "")}
                              onClick={() => {
                                if (e.is_dir) setBrowseDir(p);
                                else {
                                  setFilePath(p);
                                  setPathInput(contractHome(p, home));
                                }
                              }}
                            >
                              <span className="hw-tile-icon" aria-hidden="true">
                                {e.is_dir ? <Folder size={18} /> : <FileText size={18} />}
                              </span>
                              <span className="hw-appopt-name">{e.name}</span>
                            </button>
                          );
                        })}
                        {shownEntries.length > 200 ? <span className="hw-empty">Showing the first 200 — type a path above.</span> : null}
                        {!dirError && dirEntries !== null && !shownEntries.length ? <span className="hw-empty">Nothing here.</span> : null}
                      </div>
                    </div>
                    {filePath ? (
                      <div className={"hw-filepick-status" + (fileCheck?.path === filePath ? (fileCheck.ok ? " is-ok" : " is-err") : "")}>
                        {fileCheck?.path !== filePath
                          ? "Checking…"
                          : fileCheck.ok
                            ? `Will show as ${fileCheck.mode}`
                            : (fileCheck.reason ?? "The explorer has no view for this file.")}
                      </div>
                    ) : null}
                  </>
                ) : appsState.error ? (
                  <div className="hw-nofolders">
                    <SourceIcon source="app" large />
                    <b>Couldn't load apps.</b>
                    <button type="button" className="hw-btn" onClick={appsState.retry}>
                      Retry
                    </button>
                  </div>
                ) : allApps === null ? (
                  <span className="hw-app-skel skel-bar" role="status" aria-busy="true" aria-label="Loading apps" />
                ) : noApps ? (
                  <div className="hw-nofolders">
                    <SourceIcon source="app" large />
                    <b>No apps yet</b>
                    <span>Create one from New app in the sidebar.</span>
                  </div>
                ) : (
                  <>
                    {allApps.length > 8 ? (
                      <input
                        className="hw-appsearch"
                        type="search"
                        placeholder="Search apps"
                        aria-label="Search apps"
                        value={appQuery}
                        onChange={(e) => setAppQuery(e.target.value)}
                      />
                    ) : null}
                    <div className="hw-apppick" aria-label="App">
                      {shownApps.map((a) => (
                        <button
                          key={a.path}
                          type="button"
                          className="hw-appopt"
                          onClick={() => commit(pending, { appPath: a.path })}
                        >
                          <AppGlyph app={a} />
                          <span className="hw-appopt-text">
                            <span className="hw-appopt-name">{appName(a)}</span>
                            <span className="hw-appopt-dir">{appFolderLine(a, home)}</span>
                          </span>
                        </button>
                      ))}
                      {!shownApps.length ? <span className="hw-empty">No apps match.</span> : null}
                    </div>
                  </>
                )}
              </div>
            )
          ) : (
            sections.map((s) => (
              <section key={s.source} className="hw-gal-sec" aria-label={SOURCES[s.source].label}>
                <div className="hw-gal-sec-head">
                  <span className="hw-label">{SOURCES[s.source].label}</span>
                  <span className="hw-gal-sec-desc">{SOURCES[s.source].description}</span>
                </div>
                {metrics ? (
                  <div className="hw-gal-row">
                    {s.entries.map((e) => (
                      <GalleryCard key={e.key} entry={e} metrics={metrics} onChoose={() => choose(e)} />
                    ))}
                  </div>
                ) : null}
              </section>
            ))
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** A stand-in model for the entry, so the real widget body can draw it. */
function previewWidget(e: GalleryEntry): Widget {
  return { id: "gallery-" + e.key, source: e.source, size: e.size, format: e.format, x: 0, y: 0 };
}

interface PreviewMetrics {
  /** Pixel width of one grid column on the page behind the sheet. */
  colW: number;
  scale: number;
}

/** The live grid's column width (the sheet split into columns when no grid is
    mounted), and the one scale that fits a full-row preview into the body. */
function measurePreviews(body: HTMLElement): PreviewMetrics {
  const avail = body.clientWidth - 40;
  const gridW = document.querySelector<HTMLElement>(".hw-grid")?.clientWidth || avail;
  const colW = Math.max(40, (gridW - (GRID_COLS - 1) * 16) / GRID_COLS);
  return { colW, scale: previewScale(previewPx(GRID_COLS, colW), avail) };
}

/** The widget at its real tile size, scaled down. Folder and page need a
    target to show anything, so they draw a stand-in. */
function GalleryPreview({ entry, metrics }: { entry: GalleryEntry; metrics: PreviewMetrics }) {
  const w = previewPx(entry.cols, metrics.colW);
  const h = previewPx(entry.rows, 52);
  const bare = entry.source === "search" || entry.source === "build";
  const needsTarget = entry.source === "folder" || entry.source === "app";
  const widget = previewWidget(entry);
  const inert = (el: HTMLElement | null) => el?.setAttribute("inert", "");
  return (
    <span className="hw-gal-prev" style={{ width: Math.round(w * metrics.scale), height: Math.round(h * metrics.scale) }}>
      <span ref={inert} className="hw-gal-scale" aria-hidden="true" tabIndex={-1} style={{ width: w, height: h, transform: `scale(${metrics.scale})` }}>
        <section className={"hw-widget" + ` hw-size-${entry.size}` + (bare ? " is-" + entry.source : "") + (needsTarget ? " is-ph" : "")} style={{ width: w, height: h }}>
          {bare ? null : (
            <div className="hw-head">
              <h2 className="hw-title">{SOURCES[entry.source].label}</h2>
            </div>
          )}
          {needsTarget ? (
            <div className="hw-gal-ph">
              <SourceIcon source={entry.source} large />
              <span>{entry.source === "folder" ? "Choose a bookmark folder" : "Choose an app, file or site"}</span>
            </div>
          ) : (
            <WidgetBody widget={widget} edit={bare} onRemove={() => {}} />
          )}
        </section>
      </span>
    </span>
  );
}

function GalleryCard({ entry, metrics, onChoose }: { entry: GalleryEntry; metrics: PreviewMetrics; onChoose: () => void }) {
  const w = Math.round(previewPx(entry.cols, metrics.colW) * metrics.scale);
  return (
    <button type="button" className="hw-gal-entry" style={{ width: w }} disabled={!!entry.disabled} onClick={onChoose} title={entry.disabled ?? entry.title}>
      <GalleryPreview entry={entry} metrics={metrics} />
      <span className="hw-gal-cap">
        {entry.formatLabel ? <span className="hw-gal-title">{entry.formatLabel}</span> : null}
        <span className="hw-gal-fp">{entry.formatLabel ? "· " : ""}{entry.footprint}</span>
      </span>
      {entry.disabled ? <span className="hw-gal-fp">{entry.disabled}</span> : null}
    </button>
  );
}
