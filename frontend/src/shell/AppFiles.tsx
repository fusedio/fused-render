// The app page's Files tab: the app folder's files as a tree
// on the left, the selected file rendered in one of ITS templates on the right,
// with the file's name and a template switcher above the render.
//
// Not the explorer, again: no listing chrome, no crumb, no file operations —
// this answers "what is in this app and what does each piece look like", and
// the explorer stays one caption-click away for everything else. What it
// borrows from the explorer is the RULES, not the components: the template
// list is the server's (`stat.templates`, SPEC PT-8), which template is
// visible/default/active is mode-visibility.ts (the one policy every mode
// surface shares), and the iframe URL is built the way Preview.tsx builds it
// (`/render?path=<template>&_file=<file>`, or `/render?path=<file>` for the
// `_render` sentinel). Companion modes (`claude`, and the folder-borrowed
// `git`/`mcp`) are dropped: they talk ABOUT a file, and this pane shows one.
//
// The tree is ONE `walkDir` call (breadth-first, gitignore-pruned, capped
// server-side) assembled from `rel`, not a listing per folder: an app folder is
// small, and one request paints the whole tree at once. A truncated walk says
// so in a caption rather than pretending it saw everything.
//
// Selection lives in the QUERY — `?file=<rel>` and `?_mode=<mode>` — written
// in place (replaceSearch: a click on a file is not a navigation the Back
// button should have to retrace) and read back on every URL event. The app
// page carries the query across a tab switch untouched, so leaving for Tasks
// and coming back finds the same file in the same template. The rel is
// validated before it is joined to the folder: this page's routes are bare
// shell fallbacks, so the client is the only guard (same posture as
// slugFromAppPath).
import { useEffect, useMemo, useState } from "react";
import {
  resolveConditions,
  statPath,
  walkDir,
  type TemplateEntry,
} from "@platform/lib/api";
import { useUrlVersion } from "@platform/lib/hooks";
import { replaceSearch } from "@platform/lib/router";
import {
  defaultMode,
  effectiveActive,
  visibleModes,
  type ConditionVerdicts,
} from "@platform/lib/mode-visibility";
import { iconForEntry } from "@platform/ui/FileIcons";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { ChevronRight, FileSearch } from "lucide-react";
import { cn } from "@platform/lib/utils";
import { basename, formatSize } from "@platform/lib/format";
import { modeTitle, templateModeIcon } from "@apps/explorer/ModeSwitcher";
import {
  ancestorsOf,
  buildTree,
  contentTemplates,
  fileCount,
  renderSrc,
  safeRel,
  type TreeNode,
} from "./app-files-lib";

// ---- the component ----------------------------------------------------------

type Walk =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; nodes: TreeNode[]; truncated: boolean };

type Stat =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "ok"; templates: TemplateEntry[] };

export default function AppFiles({
  dir,
  entry,
  folderHref,
}: {
  /** The app folder, absolute forward-slash. */
  dir: string;
  /** The app's entry page (absolute) — the default selection. */
  entry: string | null;
  folderHref: string;
}) {
  useUrlVersion();
  const params = new URLSearchParams(location.search);
  const entryRel =
    entry && entry.startsWith(dir + "/") ? entry.slice(dir.length + 1) : null;
  const rel = safeRel(params.get("file")) ?? entryRel;
  const requestedMode = params.get("_mode");
  const file = rel ? dir + "/" + rel : null;

  const [walk, setWalk] = useState<Walk>({ kind: "loading" });
  const [open, setOpen] = useState<Set<string>>(() => new Set(rel ? ancestorsOf(rel) : []));
  const [stat, setStat] = useState<Stat>({ kind: "loading" });
  const [verdicts, setVerdicts] = useState<ConditionVerdicts>(null);

  useEffect(() => {
    let live = true;
    setWalk({ kind: "loading" });
    walkDir(dir)
      .then((r) => {
        if (live) setWalk({ kind: "ok", nodes: buildTree(r.entries), truncated: r.truncated });
      })
      .catch((e) => {
        if (live) setWalk({ kind: "error", message: (e as Error).message });
      });
    return () => {
      live = false;
    };
  }, [dir]);

  // The selected file's templates, and the verdicts for any gated ones (CT-12:
  // stat only marks them; the gates run here, in the background).
  useEffect(() => {
    if (!file) return;
    let live = true;
    setStat({ kind: "loading" });
    setVerdicts(null);
    statPath(file)
      .then((st) => {
        if (!live) return;
        const templates = contentTemplates(st.templates);
        setStat({ kind: "ok", templates });
        if (templates.some((t) => t.conditional)) {
          resolveConditions(file)
            .then((r) => live && setVerdicts(r.conditions))
            .catch(() => live && setVerdicts({}));
        } else {
          setVerdicts({});
        }
      })
      .catch((e) => {
        if (live) setStat({ kind: "error", message: (e as Error).message });
      });
    return () => {
      live = false;
    };
  }, [file]);

  // Opening a file also opens the folders above it, so a `?file=` deep link
  // lands on a visible row.
  useEffect(() => {
    if (!rel) return;
    const need = ancestorsOf(rel);
    if (need.every((a) => open.has(a))) return;
    setOpen((prev) => new Set([...prev, ...need]));
  }, [rel]); // eslint-disable-line react-hooks/exhaustive-deps

  const setQuery = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(location.search);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null) next.delete(k);
      else next.set(k, v);
    }
    const q = next.toString();
    // main.tsx wraps replaceState to fire fused:urlchange, which useUrlVersion
    // above listens for — so this re-renders without a second event.
    replaceSearch(location.pathname + (q ? "?" + q : ""));
  };

  const pickFile = (nextRel: string) => {
    if (nextRel === rel) return;
    // A new file starts on ITS default template: `_mode` is per file, and a
    // CSV's "table" means nothing to the HTML beside it.
    setQuery({ file: nextRel, _mode: null });
  };

  const toggleDir = (dirRel: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(dirRel)) next.delete(dirRel);
      else next.add(dirRel);
      return next;
    });

  const visible = useMemo(
    () => (stat.kind === "ok" ? visibleModes(stat.templates, verdicts) : []),
    [stat, verdicts],
  );
  const active = effectiveActive(visible, requestedMode);
  const dflt = defaultMode(visible);

  const pickMode = (mode: string) => {
    if (!active || mode === active.mode) return;
    // The default mode is the clean URL (Preview's rule): no `_mode` at all.
    setQuery({ _mode: dflt && mode === dflt.mode ? null : mode });
  };

  // One tree row. Folders wear their file count, files their size — the two
  // facts a reader scans a tree for. Nested levels sit inside an indent guide
  // (the branch `ul`'s left rule, CSS) rather than behind ever-longer padding,
  // so depth reads as structure instead of whitespace.
  const renderNodes = (nodes: TreeNode[]) =>
    nodes.map((n) => {
      const isOpen = n.isDir && open.has(n.rel);
      const selected = !n.isDir && n.rel === rel;
      return (
        <li key={n.rel} role="treeitem" aria-expanded={n.isDir ? isOpen : undefined}>
          <button
            type="button"
            className={cn("app-files-row", selected && "is-selected")}
            onClick={() => (n.isDir ? toggleDir(n.rel) : pickFile(n.rel))}
            aria-current={selected ? "true" : undefined}
            title={n.rel}
          >
            <span className="app-files-twist" aria-hidden>
              {n.isDir && <ChevronRight className={cn(isOpen && "is-open")} />}
            </span>
            <span className="app-files-icon">{iconForEntry(n.name, n.isDir)}</span>
            <span className="app-files-name">{n.name}</span>
            <span className="app-files-meta">
              {n.isDir ? fileCount(n) : formatSize(n.size)}
            </span>
          </button>
          {n.isDir && isOpen && n.children.length > 0 && (
            <ul role="group" className="app-files-branch">
              {renderNodes(n.children)}
            </ul>
          )}
        </li>
      );
    });

  const nodes = walk.kind === "ok" ? walk.nodes : [];
  const total =
    walk.kind === "ok" ? nodes.reduce((a, n) => a + (n.isDir ? fileCount(n) : 1), 0) : null;
  const selectedNode = rel ? findNode(nodes, rel) : null;
  const name = rel ? basename(rel) : "";

  return (
    <div className="app-files">
      {/* The tree column: a quieter plate than the view, one rule between. */}
      <nav className="app-files-tree" aria-label="App files">
        <div className="app-files-tree-head">
          <span className="app-files-eyebrow">Files</span>
          {total !== null && <span className="app-files-meta">{total}</span>}
        </div>
        <div className="app-files-tree-body">
          {walk.kind === "loading" && <SkeletonLines rows={4} label="Listing files" />}
          {walk.kind === "error" && (
            <ErrorBanner>Could not list files: {walk.message}</ErrorBanner>
          )}
          {walk.kind === "ok" && nodes.length === 0 && (
            <p className="app-files-caption">Nothing here yet.</p>
          )}
          {walk.kind === "ok" && nodes.length > 0 && <ul role="tree">{renderNodes(nodes)}</ul>}
          {walk.kind === "ok" && walk.truncated && (
            <p className="app-files-caption">
              Showing the first {total} files. <a href={folderHref}>Open the folder</a> for the
              rest.
            </p>
          )}
        </div>
      </nav>

      <section className="app-files-view">
        {!file && (
          <div className="app-files-blank">
            <FileSearch aria-hidden />
            <p>Pick a file to see it here.</p>
          </div>
        )}
        {file && (
          <>
            {/* The view's header: where the file sits in the folder, how big
                it is, and which of its templates is drawing it. The switcher
                is the one control on the row; a switcher of one hides itself. */}
            <header className="app-files-head">
              <div className="app-files-title">
                <span className="app-files-icon">{iconForEntry(name, false)}</span>
                <h2 title={file}>
                  {ancestorsOf(rel ?? "").map((a) => (
                    <span key={a} className="app-files-crumb">
                      {a.slice(a.lastIndexOf("/") + 1)}
                      <span className="app-files-slash">/</span>
                    </span>
                  ))}
                  {name}
                </h2>
                {selectedNode?.size != null && (
                  <span className="app-files-meta">{formatSize(selectedNode.size)}</span>
                )}
              </div>
              {active && visible.length > 1 && (
                <Tabs value={active.mode} onValueChange={(v) => pickMode(String(v))}>
                  <TabsList aria-label="Template" className="h-7">
                    {visible.map((t) => (
                      <TabsTrigger
                        key={t.mode}
                        value={t.mode}
                        className="px-2 text-xs"
                        disabled={!!t.conditional && verdicts === null}
                        title={modeTitle(t.mode)}
                      >
                        <span className="app-files-mode-icon" data-icon="inline-start">
                          {templateModeIcon(t)}
                        </span>
                        {modeTitle(t.mode)}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </Tabs>
              )}
            </header>
            <div className="app-files-stage">
              {stat.kind === "loading" && <SkeletonLines rows={2} label="Loading file" />}
              {stat.kind === "error" && (
                <ErrorBanner>
                  Could not open {rel}: {stat.message}
                </ErrorBanner>
              )}
              {stat.kind === "ok" && !active && (
                <div className="app-files-blank">
                  <p>No template can show this file.</p>
                </div>
              )}
              {stat.kind === "ok" && active && (
                <iframe
                  key={file + "|" + active.mode}
                  className="app-files-frame"
                  src={renderSrc(file, active)}
                  title={`${rel} — ${modeTitle(active.mode)}`}
                />
              )}
            </div>
          </>
        )}
      </section>
    </div>
  );
}

function findNode(nodes: TreeNode[], rel: string): TreeNode | null {
  for (const n of nodes) {
    if (n.rel === rel) return n;
    if (n.isDir && rel.startsWith(n.rel + "/")) return findNode(n.children, rel);
  }
  return null;
}

