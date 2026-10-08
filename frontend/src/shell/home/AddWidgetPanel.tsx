// The "Add a widget" sheet: a centred modal with the sources on the left and,
// on the right, a large preview of the chosen look plus the format and size
// picks. "Add to Home" appends with the chosen format and size. Opened from a
// tile's Change popover (`target`) it configures one folder or page for that
// tile instead: no size, and "Put in this tile" swaps or fills it.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
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
  type LucideIcon,
} from "lucide-react";
import { createPortal } from "react-dom";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { isFolder, loadBookmarks, type BookmarkFolder, type BookmarkItem } from "@platform/lib/bookmarks";
import { FormatPreview } from "./FormatPreview";
import { FormatPicks, SizeChips, SortChips, stageZoom } from "./Pickers";
import { MAX_WIDGETS, SOURCES, hasSearch, type AppsSort, type TileTarget, type WidgetFormat, type WidgetSize, type WidgetSource } from "./layout";
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

const ALL_SOURCE_KEYS = Object.keys(SOURCES) as WidgetSource[];

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
  const list = useRef<HTMLDivElement>(null);
  // One search box per Home: its source is not offered once it is there.
  const SOURCE_KEYS = target
    ? [initialSource ?? "folder"]
    : ALL_SOURCE_KEYS.filter((s) => s !== "search" || !hasSearch(api.layout));
  const [source, setSource] = useState<WidgetSource>(SOURCE_KEYS[0]);
  const [format, setFormat] = useState<WidgetFormat>(SOURCES[SOURCE_KEYS[0]].formats[0]);
  const [size, setSize] = useState<WidgetSize>(SOURCES[SOURCE_KEYS[0]].sizes[0]);
  const [sort, setSort] = useState<AppsSort>("opened");
  const [folderId, setFolderId] = useState<string | null>(null);
  const [appPath, setAppPath] = useState<string | null>(null);
  const [appQuery, setAppQuery] = useState("");
  const appsState = useAllApps(source === "app");
  const home = useHome();
  const rawApps = appsState.apps;
  const allApps = useMemo(() => (rawApps ? pickerApps(rawApps) : null), [rawApps]);
  const chosenApp = allApps?.find((a) => a.path === appPath) ?? allApps?.[0] ?? null;
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
  const spec = SOURCES[source];
  const full = (!target || target.kind === "fill") && api.layout.widgets.length >= MAX_WIDGETS;
  const chosenFolder = folders.find((f) => f.id === folderId) ?? folders[0] ?? null;
  const noFolders = source === "folder" && !folders.length;

  const pick = (s: WidgetSource) => {
    setSource(s);
    setFormat(SOURCES[s].formats[0]);
    setSize(SOURCES[s].sizes[0]);
    setSort("opened");
  };

  // Focus the dialog on open; hand focus back to whatever opened it on close.
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    dialog.current?.focus();
    return () => opener?.focus?.();
  }, []);

  useEffect(() => {
    const key = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      onClose();
    };
    document.addEventListener("keydown", key, true);
    return () => document.removeEventListener("keydown", key, true);
  }, [onClose]);

  const onListKey = (e: KeyboardEvent) => {
    const d = e.key === "ArrowDown" ? 1 : e.key === "ArrowUp" ? -1 : 0;
    if (!d) return;
    e.preventDefault();
    const next = SOURCE_KEYS[SOURCE_KEYS.indexOf(source) + d];
    if (!next) return;
    pick(next);
    list.current?.querySelector<HTMLElement>(`[data-source="${next}"]`)?.focus();
  };

  // Keep Tab inside the dialog.
  const onDialogKey = (e: KeyboardEvent) => {
    if (e.key !== "Tab" || !dialog.current) return;
    const items = Array.from(
      dialog.current.querySelectorAll<HTMLElement>("button:not(:disabled), [tabindex]:not([tabindex='-1'])"),
    );
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

  const add = () => {
    if (full || noFolders || noApps || (source === "app" && (urlMode ? !url : fileMode ? !fileOk : !chosenApp))) return;
    const opts =
      source === "folder"
        ? { folderId: chosenFolder?.id, format, size }
        : source === "app"
          ? { appPath: urlMode ? url! : fileMode ? filePath : chosenApp?.path, format, size }
          : source === "apps"
            ? { format, size, sort }
            : { format, size };
    if (target) {
      const { size: _size, ...tileOpts } = opts;
      if (target.kind === "swap") api.swap(target.widget.id, source, tileOpts);
      else api.fill(target.rect, source, tileOpts);
      onClose();
      return;
    }
    api.add(source, opts);
    onClose();
    // The new widget is the last one in the grid; wait a beat for it to mount.
    setTimeout(() => {
      const all = document.querySelectorAll(".hw-widget");
      all[all.length - 1]?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }, 80);
  };

  return createPortal(
    <div className="hw-scrim" onPointerDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={dialog}
        className="hw-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="Add a widget"
        tabIndex={-1}
        onKeyDown={onDialogKey}
      >
        <div className="hw-sheet-body">
          <div className="hw-sheet-side" role="listbox" aria-label="Widget source" ref={list} onKeyDown={onListKey}>
            {SOURCE_KEYS.map((s) => (
              <button
                key={s}
                type="button"
                role="option"
                aria-selected={s === source}
                data-source={s}
                tabIndex={s === source ? 0 : -1}
                className={"hw-src" + (s === source ? " is-on" : "")}
                onClick={() => pick(s)}
              >
                <SourceIcon source={s} />
                {SOURCES[s].label}
              </button>
            ))}
          </div>
          <div className="hw-sheet-main">
            <div>
              <h3 className="hw-sheet-title">{spec.label}</h3>
              <p className="hw-sheet-desc">{spec.description}</p>
            </div>
            {full ? <p className="hw-sheet-note">Home is full ({MAX_WIDGETS} widgets). Remove one to add another.</p> : null}
            <div className="hw-sheet-cfg">
            {source === "folder" ? (
              <div className="hw-stage-box is-folders">
                {folders.length ? (
                  <div className="hw-folders" role="radiogroup" aria-label="Bookmark folder">
                    {folders.map((f) => (
                      <button
                        key={f.id}
                        type="button"
                        role="radio"
                        aria-checked={f.id === chosenFolder?.id}
                        className={"hw-folder" + (f.id === chosenFolder?.id ? " is-on" : "")}
                        onClick={() => setFolderId(f.id)}
                      >
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
            ) : source === "app" ? (
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
                    <div className="hw-apppick" role="radiogroup" aria-label="App">
                      {shownApps.map((a) => (
                        <button
                          key={a.path}
                          type="button"
                          role="radio"
                          aria-checked={a.path === chosenApp?.path}
                          className={"hw-appopt" + (a.path === chosenApp?.path ? " is-on" : "")}
                          onClick={() => setAppPath(a.path)}
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
            ) : (
              <div className="hw-stage-box">
                <div className="hw-stage-card">
                  <div className="hw-stage-card-head">
                    {spec.label}
                    <span>See all ›</span>
                  </div>
                  <span className="hw-stage-in" style={{ zoom: stageZoom(source, format) }}>
                    <FormatPreview source={source} format={format} />
                  </span>
                </div>
              </div>
            )}
            <div className="hw-opts">
              {spec.formats.length > 1 ? (
                <div className="hw-opt">
                  <span className="hw-label">Show as</span>
                  <FormatPicks source={source} formats={spec.formats} value={format} onChange={setFormat} />
                </div>
              ) : null}
              {target ? null : (
                <div className="hw-opt">
                  <span className="hw-label">Size</span>
                  <SizeChips sizes={spec.sizes} value={size} onChange={setSize} />
                </div>
              )}
              {source === "apps" ? (
                <div className="hw-opt">
                  <span className="hw-label">Sort by</span>
                  <SortChips value={sort} onChange={setSort} />
                </div>
              ) : null}
            </div>
            </div>
          </div>
        </div>
        <div className="hw-sheet-foot">
          <button type="button" className="hw-tb is-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="hw-tb is-primary" disabled={full || noFolders || noApps || (source === "app" && (urlMode ? !url : fileMode ? !fileOk : !chosenApp))} onClick={add}>
            {target ? "Put in this tile" : "Add to Home"}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
