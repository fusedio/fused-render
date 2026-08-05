// Right-hand preview pane for the directory listing (views/Listing.tsx) —
// selection-driven: previews the listing's lead row when exactly one row is
// selected, and shows a neutral placeholder otherwise. Reuses the app's own
// template pipeline for FILES AND FOLDERS alike: the selection is stat'ed to
// discover its template modes and the default embeds as the normal /render
// iframe. A folder whose default resolves to the `_listing` sentinel (no
// custom directory template) falls back to the lightweight in-pane view: its
// lone HTML "app" rendered in place, or a read-only mini list of children.
// The header carries a PaneModeMenu so the previewed mode can be switched
// (pane-local, transient — it never touches the URL or saved viewstate).
// Wire-up state (pane visibility, width, the divider drag) stays in Listing —
// this component only owns what the pane shows for a given selection.
import { useEffect, useState } from "react";
import { listDir, resolveConditions, statPath } from "@platform/lib/api";
import type { FsEntry, TemplateEntry } from "@platform/lib/api";
import { navigate, VIEW_PREFIX } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";
import PaneModeMenu from "@apps/explorer/PaneModeMenu";

// The selected row, as the pane needs it. Structurally a subset of Listing's
// RowCtx, so the lead row can be passed straight through.
export interface PaneTarget {
  path: string;
  name: string;
  isDir: boolean;
}

// A folder child with its absolute path attached (listDir returns bare names).
interface PaneChild extends FsEntry {
  path: string;
}

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

// The `_listing` fallback for a folder: lone-app iframe or child mini-list.
type DirBody =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "app"; src: string; app: PaneChild }
  | { status: "children"; children: PaneChild[]; truncated: boolean };

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

// Mini-list order: dot-entries last, dirs first within each, then name asc —
// the pane is a glance, not a sortable table, so the order is fixed.
function sortChildren(children: PaneChild[]): PaneChild[] {
  return [...children].sort((a, b) => {
    const aDot = a.name.startsWith(".");
    const bDot = b.name.startsWith(".");
    if (aDot !== bDot) return aDot ? 1 : -1;
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  });
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
  // Pane-local mode override from the mode menu; null = the default mode.
  // The component is keyed on the previewed path (Listing), so this resets
  // with every new selection — deliberately transient.
  const [modeOverride, setModeOverride] = useState<string | null>(null);
  const [dirBody, setDirBody] = useState<DirBody>({ status: "loading" });

  // Stat the selection — files and folders alike carry a template mode list
  // (folders at minimum the `_listing` sentinel). The cleanup flag is the
  // superseded-click guard: a stale async result for a row that's no longer
  // selected must not render.
  const path = row?.path;
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

  // --- choose the active mode ------------------------------------------------

  // `_listing` mounts the shell's Listing component full-screen (Preview.tsx),
  // not an iframe — the pane has no iframe slot for it. For folders it maps to
  // the in-pane fallback below; on a file (a rare registry rebind) it is
  // skipped when defaulting and shows the metadata card if picked explicitly.
  let active: TemplateEntry | null = null; // entry to embed as an iframe
  let activeMode: string | null = null; // includes "_listing" (no entry embed)
  if (info.status === "ok") {
    const embeddable = info.templates.filter((e) => e.mode !== "_listing");
    const allowed = (e: TemplateEntry) =>
      !e.conditional || (info.conditions !== null && info.conditions[e.mode] === true);
    if (modeOverride !== null) {
      activeMode = modeOverride;
      active = embeddable.find((e) => e.mode === modeOverride && allowed(e)) ?? null;
    } else {
      // Same default-mode rule as Preview (CT-12): the first UNCONDITIONAL
      // entry; an all-conditional list waits for verdicts and takes the first
      // allowed one. No embeddable default → folders fall back to `_listing`,
      // files to the metadata card.
      const t = embeddable.find((e) => !e.conditional) ?? embeddable.find(allowed) ?? null;
      active = t;
      activeMode = t ? t.mode : row?.isDir ? "_listing" : null;
    }
  }

  // --- the `_listing` folder fallback -----------------------------------------

  const wantDirBody = info.status === "ok" && !!row?.isDir && activeMode === "_listing";
  useEffect(() => {
    if (!wantDirBody || !path) return;
    let alive = true;
    setDirBody({ status: "loading" });
    listDir(path).then(
      (data) => {
        if (!alive) return;
        const dir = (data.path || path).replace(/\/+$/, "");
        const children = data.entries.map((e) => ({ ...e, path: dir + "/" + e.name }));
        // A truncated listing is only a partial page (server cap), so a lone
        // HTML match in it doesn't prove it is the folder's ONLY one — fall
        // through to the child list rather than render a maybe-wrong app
        // (mirrors Listing's onSingleApp guard).
        const apps = children.filter((e) => isAppEntry(e.name, e.is_dir));
        if (!data.truncated && apps.length === 1) {
          const app = apps[0];
          setDirBody({ status: "app", src: `/render?path=${encodeURIComponent(app.path)}`, app });
        } else {
          setDirBody({
            status: "children",
            children: sortChildren(children),
            truncated: !!data.truncated,
          });
        }
      },
      (err: Error) => alive && setDirBody({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [wantDirBody, path]);

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

  // Header: name + mode menu + Open. Shown for folders and for any templated
  // view — the metadata card carries its own big-icon layout instead.
  const header = (
    <div className="pane-header">
      <span className="pane-header-icon">{iconForEntry(row.name, row.isDir)}</span>
      <span className="pane-header-name" title={row.name}>
        {row.name}
      </span>
      <PaneModeMenu
        path={row.path}
        query={modeOverride !== null ? `?_mode=${encodeURIComponent(modeOverride)}` : ""}
        onNavigate={(q) =>
          setModeOverride(new URLSearchParams(q.replace(/^\?/, "")).get("_mode"))
        }
      />
      <button
        type="button"
        className="pane-header-btn"
        onClick={() => navigate(row.path, { isDir: row.isDir })}
      >
        Open
      </button>
    </div>
  );

  // The /render embed URL for a chosen template entry. "_render" renders the
  // file itself (PT-12); a template mode renders the template against _file.
  const srcFor = (t: TemplateEntry): string => {
    if (t.mode === "_render") return `/render?path=${encodeURIComponent(row.path)}`;
    const remote = info.status === "ok" && info.remote ? "&_remote=1" : "";
    return `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(row.path)}${remote}`;
  };

  let body: React.ReactNode;
  if (info.status === "loading") {
    body = <div className="pane-skel" />;
  } else if (info.status === "error") {
    body = (
      <div className="pane-center">
        <div className="status-message error">{info.message}</div>
      </div>
    );
  } else if (active) {
    // Keyed on mode so switching modes replaces the iframe outright — never
    // reuse a live frame across different templates.
    body = (
      <>
        {header}
        <iframe key={active.mode} className="pane-frame" src={srcFor(active)} title={row.name} />
      </>
    );
  } else if (row.isDir && activeMode === "_listing") {
    if (dirBody.status === "loading") {
      body = (
        <>
          {header}
          <div className="pane-skel" />
        </>
      );
    } else if (dirBody.status === "error") {
      body = (
        <>
          {header}
          <div className="pane-center">
            <div className="status-message error">{dirBody.message}</div>
          </div>
        </>
      );
    } else if (dirBody.status === "app") {
      body = (
        <>
          <div className="pane-header">
            <span className="pane-header-icon">{iconForEntry(dirBody.app.name, false)}</span>
            <span className="pane-header-name" title={dirBody.app.name}>
              {dirBody.app.name}
            </span>
            <PaneModeMenu
              path={row.path}
              query={modeOverride !== null ? `?_mode=${encodeURIComponent(modeOverride)}` : ""}
              onNavigate={(q) =>
                setModeOverride(new URLSearchParams(q.replace(/^\?/, "")).get("_mode"))
              }
            />
            <button
              type="button"
              className="pane-header-btn"
              onClick={() => window.open(viewUrlFor(dirBody.app.path), "_blank", "noopener")}
            >
              Open app
            </button>
          </div>
          <iframe className="pane-frame" src={dirBody.src} title={row.name} />
        </>
      );
    } else {
      // Folder children. Clicking a child navigates the shell to it — the user
      // is already one level in, so a click here means "go there".
      body = (
        <>
          {header}
          <div className="pane-mini-scroll">
            <table className="pane-mini-list">
              <tbody>
                {dirBody.children.length === 0 ? (
                  <tr>
                    <td colSpan={2} className="status-message">
                      Empty folder
                    </td>
                  </tr>
                ) : (
                  dirBody.children.map((c) => (
                    <tr
                      key={c.name}
                      className={c.ignored ? "row ignored" : "row"}
                      onClick={() => navigate(c.path, { isDir: c.is_dir })}
                    >
                      <td className="name">
                        <span className="icon">{iconForEntry(c.name, c.is_dir)}</span>
                        <span className="label">{c.name}</span>
                      </td>
                      <td className="size">{c.is_dir ? "" : formatSize(c.size)}</td>
                    </tr>
                  ))
                )}
                {dirBody.truncated && (
                  <tr>
                    <td colSpan={2} className="status-message">
                      Showing first {dirBody.children.length} entries — folder listing is partial.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </>
      );
    }
  } else if (
    info.templates.some((e) => e.mode !== "_listing" && e.conditional) &&
    info.conditions === null &&
    modeOverride === null
  ) {
    // All-conditional mode list, verdicts still in flight: keep the skeleton —
    // the pane must not claim "no preview" while a gate may still allow one.
    body = <div className="pane-skel" />;
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
