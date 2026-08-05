// Right-hand preview pane for the directory listing (views/Listing.tsx) —
// selection-driven: previews the listing's lead row when exactly one row is
// selected, and shows a neutral placeholder otherwise. Ported from the old
// `preview` directory template so the pane reuses the app's own template
// pipeline: a file is stat'ed to discover its default template and embedded
// as the normal /render iframe; a folder shows its lone HTML "app" rendered
// in place, or a read-only mini list of its children. Wire-up state (pane
// visibility, width, the divider drag) stays in Listing — this component only
// owns what the pane shows for a given selection.
import { useEffect, useState } from "react";
import { listDir, resolveConditions, statPath } from "@platform/lib/api";
import type { FsEntry, TemplateEntry } from "@platform/lib/api";
import { navigate, VIEW_PREFIX } from "@platform/lib/router";
import { formatSize } from "@platform/lib/format";
import { iconForEntry, isAppEntry } from "@platform/ui/FileIcons";
import { KNOWN_SENTINEL_MODES } from "@apps/explorer/ModeSwitcher";

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

// What the pane is currently showing for the selected row. Every async
// resolution (stat for a file, listDir for a folder) lands in exactly one of
// these; the loading skeleton covers the gap.
type PaneState =
  | { status: "loading" }
  // A file with a bound template, or a folder's lone HTML app: an embedded
  // /render iframe. The app case also carries a slim header (name + Open app).
  | { status: "frame"; src: string; app: PaneChild | null }
  // A file with no bound template: metadata card (icon, name, size, Open).
  | { status: "meta"; size: number | null }
  // A folder without a single app: its children, read-only.
  | { status: "children"; children: PaneChild[]; truncated: boolean }
  | { status: "error"; message: string };

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
  const [state, setState] = useState<PaneState>({ status: "loading" });

  // Resolve what the selected row previews as. The cleanup flag is the
  // superseded-click guard: a stale async result for a row that's no longer
  // selected must not render (the effect for the NEW row has already reset
  // state to loading).
  const path = row?.path;
  const isDir = row?.isDir;
  useEffect(() => {
    if (!path) return;
    let alive = true;
    setState({ status: "loading" });
    if (isDir) {
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
            setState({
              status: "frame",
              src: `/render?path=${encodeURIComponent(app.path)}`,
              app,
            });
          } else {
            setState({ status: "children", children: sortChildren(children), truncated: !!data.truncated });
          }
        },
        (err: Error) => alive && setState({ status: "error", message: err.message })
      );
    } else {
      statPath(path).then(
        (st) => {
          if (!alive) return;
          const show = (t: TemplateEntry) => {
            const remote = st.remote ? "&_remote=1" : "";
            const src =
              t.mode === "_render"
                ? `/render?path=${encodeURIComponent(path)}`
                : `/render?path=${encodeURIComponent(t.path as string)}&_file=${encodeURIComponent(path)}${remote}`;
            setState({ status: "frame", src, app: null });
          };
          // Same defensive filter as Preview (SPEC PT-12): an entry with
          // path===null whose mode isn't a recognized sentinel would build a
          // `path=null` render URL, so drop it before choosing.
          const templates = st.templates.filter(
            (e) => e.path !== null || KNOWN_SENTINEL_MODES.has(e.mode)
          );
          // "_listing" mounts the shell's Listing component full-screen
          // (Preview.tsx), not an iframe — the pane has no slot for that, so
          // a registry bind putting "_listing" on a file (rare, but legal)
          // must not fall into `show()` and build a `path=null` render URL.
          // Skip it here; the pane still tries the file's other templates.
          const embeddable = templates.filter((e) => e.mode !== "_listing");
          // Same default-mode rule as Preview (CT-12): the first UNCONDITIONAL
          // entry, which renders without waiting on any gate.
          const t = embeddable.find((e) => !e.conditional);
          if (t) {
            show(t);
            return;
          }
          // No unconditional entry. An ALL-conditional list still previews
          // full-screen (Preview.defaultTemplate falls back to templates[0]
          // once verdicts land), so the pane must not claim "no preview" —
          // resolve the gates and show the first allowed one. Fail closed on
          // an empty list or a broken gate: metadata card.
          if (embeddable.length === 0) {
            setState({ status: "meta", size: st.size });
            return;
          }
          resolveConditions(path).then(
            (r) => {
              if (!alive) return;
              const allowed = embeddable.find((e) => r.conditions[e.mode] === true);
              if (allowed) show(allowed);
              else setState({ status: "meta", size: st.size });
            },
            () => alive && setState({ status: "meta", size: st.size })
          );
        },
        (err: Error) => alive && setState({ status: "error", message: err.message })
      );
    }
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

  let body: React.ReactNode;
  if (state.status === "loading") {
    body = <div className="pane-skel" />;
  } else if (state.status === "error") {
    body = (
      <div className="pane-center">
        <div className="status-message error">{state.message}</div>
      </div>
    );
  } else if (state.status === "frame") {
    body = (
      <>
        {state.app && (
          <div className="pane-header">
            <span className="pane-header-icon">{iconForEntry(state.app.name, false)}</span>
            <span className="pane-header-name" title={state.app.name}>
              {state.app.name}
            </span>
            <button
              type="button"
              className="pane-header-btn"
              onClick={() => window.open(viewUrlFor(state.app!.path), "_blank", "noopener")}
            >
              Open app
            </button>
          </div>
        )}
        <iframe className="pane-frame" src={state.src} title={row.name} />
      </>
    );
  } else if (state.status === "meta") {
    body = (
      <div className="pane-center">
        <div className="pane-big-icon">{iconForEntry(row.name, false)}</div>
        <div className="pane-title">{row.name}</div>
        <div className="pane-hint">
          No preview for this file type{state.size !== null ? ` — ${formatSize(state.size)}` : ""}.
        </div>
        <button type="button" className="pane-open-btn" onClick={() => navigate(row.path, { isDir: false })}>
          Open
        </button>
      </div>
    );
  } else {
    // Folder children. Clicking a child navigates the shell to it — the user
    // is already one level in, so a click here means "go there".
    body = (
      <>
        <div className="pane-header">
          <span className="pane-header-icon">{iconForEntry(row.name, true)}</span>
          <span className="pane-header-name" title={row.name}>
            {row.name}
          </span>
          <button
            type="button"
            className="pane-header-btn"
            onClick={() => navigate(row.path, { isDir: true })}
          >
            Open folder
          </button>
        </div>
        <div className="pane-mini-scroll">
          <table className="pane-mini-list">
            <tbody>
              {state.children.length === 0 ? (
                <tr>
                  <td colSpan={2} className="status-message">
                    Empty folder
                  </td>
                </tr>
              ) : (
                state.children.map((c) => (
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
              {state.truncated && (
                <tr>
                  <td colSpan={2} className="status-message">
                    Showing first {state.children.length} entries — folder listing is partial.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </>
    );
  }

  return <div className="listing-pane">{body}</div>;
}
