// Right-hand preview pane for the directory listing (views/Listing.tsx) —
// selection-driven: previews the listing's lead row when exactly one row is
// selected, and shows a neutral placeholder otherwise. Reuses the app's own
// template pipeline for FILES AND FOLDERS alike: the selection is stat'ed to
// discover its template modes and the default embeds as the normal /render
// iframe. A folder's `_listing` mode mounts the real shell Listing component
// (embedded — no URL writes, no nested pane, no global keyboard); a folder
// with a lone top-level HTML "app" additionally offers the pane-only `_app`
// mode that renders that app in place. The header carries a mode menu so the
// previewed mode can be switched (pane-local, transient — it never touches
// the URL or saved viewstate). Wire-up state (pane visibility, width, the
// divider drag) stays in Listing — this component only owns what the pane
// shows for a given selection.
import { useEffect, useRef, useState, type MouseEvent } from "react";
import { listDir, resolveConditions, statPath } from "@platform/lib/api";
import type { TemplateEntry } from "@platform/lib/api";
import { navigate, VIEW_PREFIX } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES, modeTitle, templateModeIcon } from "@apps/explorer/ModeSwitcher";
import Listing from "@apps/explorer/Listing";

// The selected row, as the pane needs it. Structurally a subset of Listing's
// RowCtx, so the lead row can be passed straight through.
export interface PaneTarget {
  path: string;
  name: string;
  isDir: boolean;
}

// The pane-only sentinel for a folder's lone HTML app rendered in place. Not
// a registry mode — it exists only in this menu, so the constant is local.
const APP_MODE = "_app";

// The stat'ed template picture for the selected row. `conditions` is null
// while gated entries are still resolving (CT-12 deferred verdicts).
interface PaneInfo {
  templates: TemplateEntry[];
  conditions: Record<string, boolean> | null;
  size: number | null;
  remote: boolean;
}

type InfoState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | ({ status: "ok" } & PaneInfo);

// Lone-app detection for a folder: null while loading or when the folder has
// no single unambiguous app (a truncated listing never proves uniqueness).
interface AppTarget {
  name: string;
  path: string;
}

// One entry of the pane's mode menu (template modes + the pane's sentinels).
interface PaneMode {
  mode: string;
  title: string;
  icon: React.ReactNode;
}

// Always the plain /view/ route, never /embed/ — an embed URL is meant to be
// framed by a host page, so it would be the wrong thing to land in a fresh
// tab. Same drive-letter normalization as lib/router's urlForFsPath.
function viewUrlFor(fsPath: string): string {
  const norm = /^[A-Za-z]:[\\/]/.test(fsPath) ? fsPath.replace(/\\/g, "/") : fsPath;
  const encoded = norm
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return VIEW_PREFIX + encoded;
}

// The pane's own mode dropdown. PaneModeMenu (Panel/Tabs chrome) stats its
// path itself and only knows registry modes; the pane's list is richer (the
// synthetic `_app` mode) and already stat'ed here, so this menu is fed the
// finished list instead. Same look: pane-mode-* classes, fixed-position
// dropdown, closes on outside pointerdown / window blur.
function PaneModeSelect({
  modes,
  active,
  onSelect,
}: {
  modes: PaneMode[];
  active: string;
  onSelect: (mode: string) => void;
}) {
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const rootRef = useRef<HTMLSpanElement | null>(null);

  useEffect(() => {
    if (!pos) return;
    const onDown = (e: PointerEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setPos(null);
    };
    const onBlur = () => setPos(null);
    document.addEventListener("pointerdown", onDown);
    window.addEventListener("blur", onBlur);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      window.removeEventListener("blur", onBlur);
    };
  }, [pos]);

  if (modes.length < 2) return null;
  const current = modes.find((m) => m.mode === active) ?? modes[0];

  const toggle = (e: MouseEvent) => {
    e.stopPropagation();
    if (pos) {
      setPos(null);
      return;
    }
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setPos({ top: r.bottom + 4, left: Math.max(0, Math.min(r.left, window.innerWidth - 150)) });
  };

  return (
    <span className="pane-mode-menu" ref={rootRef}>
      <span className="pane-mode-btn" title={"Mode: " + current.title} onClick={toggle}>
        {current.icon}
      </span>
      {pos && (
        <span className="pane-mode-dropdown" style={{ top: pos.top, left: pos.left }}>
          {modes.map((m) => (
            <span
              key={m.mode}
              className={"pane-mode-item" + (m.mode === current.mode ? " active" : "")}
              onClick={(e) => {
                e.stopPropagation();
                setPos(null);
                onSelect(m.mode);
              }}
            >
              {m.icon}
              <span>{m.title}</span>
            </span>
          ))}
        </span>
      )}
    </span>
  );
}

export default function ListingPreviewPane({
  row,
  selCount,
}: {
  // The lead row when exactly one row is selected, else null.
  row: PaneTarget | null;
  // Total selected rows, for the multi-selection placeholder.
  selCount: number;
}) {
  const [info, setInfo] = useState<InfoState>({ status: "loading" });
  // Lone-app probe result for a folder: undefined = still loading, null =
  // no unambiguous app. Only drives the `_app` menu entry, never a default.
  const [app, setApp] = useState<AppTarget | null | undefined>(undefined);
  // Pane-local mode override from the mode menu; null = the default mode.
  // The component is keyed on the previewed path (Listing), so this resets
  // with every new selection — deliberately transient.
  const [modeOverride, setModeOverride] = useState<string | null>(null);

  // Stat the selection — files and folders alike carry a template mode list
  // (folders at minimum the `_listing` sentinel). The cleanup flag is the
  // superseded-click guard: a stale async result for a row that's no longer
  // selected must not render.
  const path = row?.path;
  const isDir = row?.isDir;
  useEffect(() => {
    if (!path) return;
    let alive = true;
    setInfo({ status: "loading" });
    statPath(path).then(
      (st) => {
        if (!alive) return;
        // Same defensive filter as Preview (SPEC PT-12): an entry with
        // path===null whose mode isn't a recognized sentinel would build a
        // `path=null` render URL, so drop it before choosing.
        const templates = st.templates.filter(
          (e) => e.path !== null || KNOWN_SENTINEL_MODES.has(e.mode)
        );
        const base: InfoState = {
          status: "ok",
          templates,
          conditions: templates.some((e) => e.conditional) ? null : {},
          size: st.size,
          remote: !!st.remote,
        };
        setInfo(base);
        if (base.conditions !== null) return;
        resolveConditions(path).then(
          (r) => alive && setInfo({ ...base, conditions: r.conditions }),
          // Fail closed, like a broken gate: every gated entry reads denied.
          () => alive && setInfo({ ...base, conditions: {} })
        );
      },
      (err: Error) => alive && setInfo({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [path]);

  // Folder lone-app probe, for the `_app` mode. A truncated listing is only a
  // partial page (server cap), so a lone HTML match in it doesn't prove it is
  // the folder's ONLY one — offer no `_app` mode then (mirrors Listing's
  // onSingleApp guard). Errors also just drop the mode; the embedded Listing
  // surfaces the folder's real error itself.
  useEffect(() => {
    if (!path || !isDir) return;
    let alive = true;
    setApp(undefined);
    listDir(path).then(
      (data) => {
        if (!alive) return;
        const dir = (data.path || path).replace(/\/+$/, "");
        const apps = data.entries.filter((e) => isAppEntry(e.name, e.is_dir));
        setApp(
          !data.truncated && apps.length === 1
            ? { name: apps[0].name, path: dir + "/" + apps[0].name }
            : null
        );
      },
      () => alive && setApp(null)
    );
    return () => {
      alive = false;
    };
  }, [path, isDir]);

  // Placeholders need no fetch: nothing selected, or a multi-selection.
  if (!row) {
    return (
      <div className="listing-pane">
        <div className="pane-center">
          <div className="pane-hint">
            {selCount > 1 ? `${selCount} items selected` : "Select a file to preview."}
          </div>
        </div>
      </div>
    );
  }

  if (info.status === "loading") {
    return (
      <div className="listing-pane">
        <div className="pane-skel" />
      </div>
    );
  }
  if (info.status === "error") {
    return (
      <div className="listing-pane">
        <div className="pane-center">
          <div className="status-message error">{info.message}</div>
        </div>
      </div>
    );
  }

  // --- the pane's mode list ---------------------------------------------------

  // Registry templates the pane can embed as an iframe. `_listing` gets its
  // own PaneMode below (folders — it mounts the embedded Listing, no iframe);
  // on a FILE a `_listing` bind (rare registry rebind) is dropped entirely:
  // the pane has no slot for a full listing of a file.
  const allowed = (e: TemplateEntry) =>
    !e.conditional || (info.conditions !== null && info.conditions[e.mode] === true);
  const embeddable = info.templates.filter((e) => e.mode !== "_listing" && allowed(e));

  const modes: PaneMode[] = embeddable.map((e) => ({
    mode: e.mode,
    title: modeTitle(e.mode),
    icon: templateModeIcon(e),
  }));
  if (row.isDir) {
    // The lone app outranks the listing: it leads the menu and wins the
    // default below when the folder has no template of its own.
    if (app) {
      modes.push({
        mode: APP_MODE,
        title: "App",
        icon: <span className="pane-header-icon">{iconForEntry(app.name, false)}</span>,
      });
    }
    modes.push({
      mode: "_listing",
      title: modeTitle("_listing"),
      icon: templateModeIcon({ mode: "_listing", path: null } as TemplateEntry),
    });
  }

  // Default mode: the folder's/file's own first eligible template (same
  // unconditional-first rule as Preview, CT-12); a folder without one falls
  // back to its lone app, then `_listing`; a file to the metadata card
  // (mode null). While the app probe is still in flight the default is
  // undecided — skeleton below, so the pane never flashes the listing and
  // then swaps to the app.
  const defaultEntry = embeddable.find((e) => !e.conditional) ?? embeddable[0] ?? null;
  if (row.isDir && !defaultEntry && modeOverride === null && app === undefined) {
    return (
      <div className="listing-pane">
        <div className="pane-skel" />
      </div>
    );
  }
  const activeMode =
    modeOverride !== null && modes.some((m) => m.mode === modeOverride)
      ? modeOverride
      : defaultEntry
        ? defaultEntry.mode
        : row.isDir
          ? app
            ? APP_MODE
            : "_listing"
          : null;
  const activeEntry = embeddable.find((e) => e.mode === activeMode) ?? null;

  // All-conditional mode list, verdicts still in flight: keep the skeleton —
  // the pane must not claim "no preview" while a gate may still allow one.
  if (
    !row.isDir &&
    activeEntry === null &&
    info.conditions === null &&
    info.templates.some((e) => e.mode !== "_listing" && e.conditional)
  ) {
    return (
      <div className="listing-pane">
        <div className="pane-skel" />
      </div>
    );
  }

  // Header: name + mode menu + Open. Shown for every mode except the file
  // metadata card, which carries its own big-icon layout instead.
  const header = (
    <div className="pane-header">
      <span className="pane-header-icon">{iconForEntry(row.name, row.isDir)}</span>
      <span className="pane-header-name" title={row.name}>
        {row.name}
      </span>
      <PaneModeSelect
        modes={modes}
        active={activeMode ?? ""}
        onSelect={(m) => setModeOverride(m)}
      />
      {activeMode === APP_MODE && app ? (
        <button
          type="button"
          className="pane-header-btn"
          onClick={() => window.open(viewUrlFor(app.path), "_blank", "noopener")}
        >
          Open app
        </button>
      ) : (
        <button
          type="button"
          className="pane-header-btn"
          onClick={() => navigate(row.path, { isDir: row.isDir })}
        >
          Open
        </button>
      )}
    </div>
  );

  // The /render embed URL for a chosen template entry. "_render" renders the
  // file itself (PT-12); a template mode renders the template against _file.
  const srcFor = (t: TemplateEntry): string => {
    if (t.mode === "_render") return `/render?path=${encodeURIComponent(row.path)}`;
    const remote = info.remote ? "&_remote=1" : "";
    return `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(row.path)}${remote}`;
  };

  let body: React.ReactNode;
  if (activeEntry) {
    // Keyed on mode so switching modes replaces the iframe outright — never
    // reuse a live frame across different templates.
    body = (
      <>
        {header}
        <iframe
          key={activeEntry.mode}
          className="pane-frame"
          src={srcFor(activeEntry)}
          title={row.name}
        />
      </>
    );
  } else if (activeMode === APP_MODE && app) {
    body = (
      <>
        {header}
        <iframe
          key={APP_MODE}
          className="pane-frame"
          src={`/render?path=${encodeURIComponent(app.path)}`}
          title={app.name}
        />
      </>
    );
  } else if (activeMode === "_listing" && row.isDir) {
    // The real shell Listing, embedded: full sorting/search/selection UI, but
    // URL-silent, paneless and without document-level keyboard (see Listing's
    // `embedded` prop).
    body = (
      <>
        {header}
        <Listing key={row.path} fsPath={row.path} embedded />
      </>
    );
  } else {
    // No embeddable template for a file: metadata card (icon, name, size, Open).
    body = (
      <div className="pane-center">
        <div className="pane-big-icon">{iconForEntry(row.name, false)}</div>
        <div className="pane-title">{row.name}</div>
        <div className="pane-hint">
          No preview for this file type{info.size !== null ? ` — ${formatSize(info.size)}` : ""}.
        </div>
        <button
          type="button"
          className="pane-open-btn"
          onClick={() => navigate(row.path, { isDir: false })}
        >
          Open
        </button>
      </div>
    );
  }

  return <div className="listing-pane">{body}</div>;
}
