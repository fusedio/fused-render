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
import { ChevronRight } from "lucide-react";
import { modeTitle, templateModeIcon } from "@apps/explorer/ModeSwitcher";
import {
  ancestorsOf,
  buildTree,
  contentTemplates,
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

  const renderNodes = (nodes: TreeNode[], depth: number) =>
    nodes.map((n) => {
      const isOpen = n.isDir && open.has(n.rel);
      return (
        <li key={n.rel} role="treeitem" aria-expanded={n.isDir ? isOpen : undefined}>
          <button
            type="button"
            className={
              "app-files-row" + (!n.isDir && n.rel === rel ? " is-selected" : "")
            }
            style={{ paddingLeft: 8 + depth * 14 }}
            onClick={() => (n.isDir ? toggleDir(n.rel) : pickFile(n.rel))}
            title={n.rel}
          >
            {n.isDir ? (
              <ChevronRight
                className={"app-files-chevron" + (isOpen ? " is-open" : "")}
                aria-hidden
              />
            ) : (
              <span className="app-files-chevron" aria-hidden />
            )}
            <span className="app-files-icon">{iconForEntry(n.name, n.isDir)}</span>
            <span className="app-files-name">{n.name}</span>
          </button>
          {n.isDir && isOpen && n.children.length > 0 && (
            <ul role="group">{renderNodes(n.children, depth + 1)}</ul>
          )}
        </li>
      );
    });

  return (
    <div className="app-files">
      <nav className="app-files-tree" aria-label="App files">
        {walk.kind === "loading" && <SkeletonLines rows={4} label="Listing files" />}
        {walk.kind === "error" && <ErrorBanner>Could not list files: {walk.message}</ErrorBanner>}
        {walk.kind === "ok" && walk.nodes.length === 0 && (
          <p className="app-page-empty">This folder is empty.</p>
        )}
        {walk.kind === "ok" && walk.nodes.length > 0 && (
          <ul role="tree">{renderNodes(walk.nodes, 0)}</ul>
        )}
        {walk.kind === "ok" && walk.truncated && (
          <p className="app-files-caption">
            Too many files to list them all — <a href={folderHref}>open the folder</a>.
          </p>
        )}
      </nav>

      <section className="app-files-view">
        {!file && (
          <p className="app-page-empty">Pick a file on the left to see it here.</p>
        )}
        {file && (
          <>
            <header className="app-files-head">
              <h2 className="app-files-title" title={file}>
                {rel}
              </h2>
              {/* The template switcher: the file's own content modes, in the
                  registry's order, the active one selected. One entry hides
                  itself — a choice of one is not a choice. */}
              {active && visible.length > 1 && (
                <Tabs value={active.mode} onValueChange={(v) => pickMode(String(v))}>
                  <TabsList aria-label="Template">
                    {visible.map((t) => (
                      <TabsTrigger
                        key={t.mode}
                        value={t.mode}
                        className="px-2"
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
            {stat.kind === "loading" && <SkeletonLines rows={2} label="Loading file" />}
            {stat.kind === "error" && (
              <ErrorBanner>Could not open {rel}: {stat.message}</ErrorBanner>
            )}
            {stat.kind === "ok" && !active && (
              <p className="app-page-empty">No template can show this file.</p>
            )}
            {stat.kind === "ok" && active && (
              <iframe
                key={file + "|" + active.mode}
                className="app-page-frame"
                src={renderSrc(file, active)}
                title={`${rel} — ${modeTitle(active.mode)}`}
              />
            )}
          </>
        )}
      </section>
    </div>
  );
}
