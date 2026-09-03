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
//
// Look: ONE bordered surface (EntityList), the way an editor's workbench is one
// window — a tree column, a single rule, then the view. The tree's rows are
// entity rows (dense, bordered, hover/selected by background shift); depth
// reads as an indent guide, not as a growing margin.
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
import { EntityList } from "@platform/ui/flow/EntityRow";
import { SectionHeading, Tiny } from "@platform/ui/flow/Typography";
import { Tabs, TabsList, TabsTrigger } from "@platform/shadcn/ui/tabs";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia } from "@platform/shadcn/ui/empty";
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

// Both columns' header rows share one height so the rule between the columns
// reads as one line across.
const HEAD = "flex h-11 flex-none items-center justify-between gap-3 border-b border-border";

// A caption under the tree (empty / truncated).
function Caption({ children }: { children: React.ReactNode }) {
  return (
    <p className="m-0 px-3 py-2 text-xs leading-relaxed text-muted-foreground [&_a]:text-inherit [&_a]:underline">
      {children}
    </p>
  );
}

/** One tree row — the entity-row vocabulary (dense, bordered by the list,
 *  hover/selected as background shifts) with the attributes a tree needs
 *  (`title`, `aria-current`) that the shared EntityRow does not pass through.
 *  Folders wear their file count, files their size — the two facts a reader
 *  scans a tree for; hidden until the row is hovered or selected so the column
 *  stays a list of names first. */
function TreeRow({
  node,
  isOpen,
  selected,
  onClick,
}: {
  node: TreeNode;
  isOpen: boolean;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-slot="entity-row"
      onClick={onClick}
      aria-current={selected ? "true" : undefined}
      title={node.rel}
      className={cn(
        "group/row flex h-7 w-full min-w-0 cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1.5 text-left text-sm text-foreground",
        "hover:bg-accent/50 focus-visible:bg-accent/50 focus-visible:outline-none",
        selected && "bg-accent/30 hover:bg-accent/40",
      )}
    >
      <span className="inline-flex w-3.5 flex-none text-muted-foreground" aria-hidden>
        {node.isDir && (
          <ChevronRight
            className={cn(
              "size-3.5 transition-transform duration-150 motion-reduce:transition-none",
              isOpen && "rotate-90",
            )}
          />
        )}
      </span>
      <span className="inline-flex size-4 flex-none text-muted-foreground [&_svg]:size-4">
        {iconForEntry(node.name, node.isDir)}
      </span>
      <span className="min-w-0 flex-1 truncate">{node.name}</span>
      <Tiny
        className={cn(
          "flex-none tabular-nums opacity-0 group-hover/row:opacity-100 group-focus-visible/row:opacity-100",
          selected && "opacity-100",
        )}
      >
        {node.isDir ? fileCount(node) : formatSize(node.size)}
      </Tiny>
    </button>
  );
}

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

  // Nested levels sit inside an indent guide (the branch `ul`'s left rule)
  // rather than behind ever-longer padding, so depth reads as structure instead
  // of whitespace.
  const renderNodes = (nodes: TreeNode[]) =>
    nodes.map((n) => {
      const isOpen = n.isDir && open.has(n.rel);
      const selected = !n.isDir && n.rel === rel;
      return (
        <li key={n.rel} role="treeitem" aria-expanded={n.isDir ? isOpen : undefined}>
          <TreeRow
            node={n}
            isOpen={isOpen}
            selected={selected}
            onClick={() => (n.isDir ? toggleDir(n.rel) : pickFile(n.rel))}
          />
          {n.isDir && isOpen && n.children.length > 0 && (
            <ul role="group" className="m-0 ml-[15px] list-none border-l border-border p-0 pl-1.5">
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
    <EntityList className="flex min-h-0 flex-1 bg-background">
      {/* The tree column: one rule between it and the view. */}
      <nav className="flex min-h-0 w-[248px] flex-none flex-col border-r border-border" aria-label="App files">
        <div className={cn(HEAD, "pr-3 pl-3.5")}>
          <SectionHeading className="text-xs">Files</SectionHeading>
          {total !== null && <Tiny className="tabular-nums">{total}</Tiny>}
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-1.5 scrollbar-auto-hide">
          {walk.kind === "loading" && <SkeletonLines rows={4} label="Listing files" />}
          {walk.kind === "error" && (
            <ErrorBanner>Could not list files: {walk.message}</ErrorBanner>
          )}
          {walk.kind === "ok" && nodes.length === 0 && <Caption>Nothing here yet.</Caption>}
          {walk.kind === "ok" && nodes.length > 0 && (
            <ul role="tree" className="m-0 list-none p-0">
              {renderNodes(nodes)}
            </ul>
          )}
          {walk.kind === "ok" && walk.truncated && (
            <Caption>
              Showing the first {total} files. <a href={folderHref}>Open the folder</a> for the
              rest.
            </Caption>
          )}
        </div>
      </nav>

      <section className="flex min-h-0 min-w-0 flex-1 flex-col">
        {!file && (
          <Empty className="border-0">
            <EmptyHeader>
              <EmptyMedia variant="icon">
                <FileSearch />
              </EmptyMedia>
              <EmptyDescription>Pick a file to see it here.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        )}
        {file && (
          <>
            {/* The view's header: where the file sits in the folder, how big
                it is, and which of its templates is drawing it. The switcher
                is the one control on the row; a switcher of one hides itself. */}
            <header className={cn(HEAD, "pr-2.5 pl-4")}>
              <div className="flex min-w-0 items-center gap-2">
                <span className="inline-flex size-4 flex-none text-muted-foreground [&_svg]:size-4">
                  {iconForEntry(name, false)}
                </span>
                <h2 className="m-0 min-w-0 truncate font-mono text-xs font-medium text-foreground" title={file}>
                  {/* The path above the name is the folder crumb, muted; the
                      name is the fact. */}
                  {ancestorsOf(rel ?? "").map((a) => (
                    <span key={a} className="font-normal text-muted-foreground">
                      {a.slice(a.lastIndexOf("/") + 1)}
                      <span className="mx-[3px] opacity-60">/</span>
                    </span>
                  ))}
                  {name}
                </h2>
                {selectedNode?.size != null && (
                  <Tiny className="tabular-nums">{formatSize(selectedNode.size)}</Tiny>
                )}
              </div>
              {/* Line variant, the same vocabulary as the page's tab strip:
                  the active template is the one underlined, the rest are
                  quiet text — no pill, no filled block in the header. The
                  switcher sits on the header's bottom rule so its active
                  underline (5px below the text) lands ON that rule. */}
              {active && visible.length > 1 && (
                <Tabs
                  value={active.mode}
                  onValueChange={(v) => pickMode(String(v))}
                  className="self-end pb-[5px]"
                >
                  <TabsList
                    variant="line"
                    aria-label="Template"
                    className="h-auto rounded-none p-0"
                  >
                    {visible.map((t) => (
                      <TabsTrigger
                        key={t.mode}
                        value={t.mode}
                        className="flex-none px-2 py-1 text-xs"
                        disabled={!!t.conditional && verdicts === null}
                        title={modeTitle(t.mode)}
                      >
                        <span
                          className="inline-flex size-3.5 [&_.mode-icon-mask]:size-3.5 [&_.mode-icon-placeholder]:size-3.5 [&_.mode-icon-placeholder]:text-[10px] [&_svg]:size-3.5"
                          data-icon="inline-start"
                        >
                          {templateModeIcon(t)}
                        </span>
                        {modeTitle(t.mode)}
                      </TabsTrigger>
                    ))}
                  </TabsList>
                </Tabs>
              )}
            </header>
            <div className="flex min-h-0 flex-1 flex-col [&>.error-banner]:m-4 [&>.skel-lines]:m-4">
              {stat.kind === "loading" && <SkeletonLines rows={2} label="Loading file" />}
              {stat.kind === "error" && (
                <ErrorBanner>
                  Could not open {rel}: {stat.message}
                </ErrorBanner>
              )}
              {stat.kind === "ok" && !active && (
                <Empty className="border-0">
                  <EmptyHeader>
                    <EmptyDescription>No template can show this file.</EmptyDescription>
                  </EmptyHeader>
                </Empty>
              )}
              {stat.kind === "ok" && active && (
                // Flush: the surface carries the border, so nothing is boxed
                // inside a box.
                <iframe
                  key={file + "|" + active.mode}
                  className="min-h-0 w-full flex-1 border-0 bg-background"
                  src={renderSrc(file, active)}
                  title={`${rel} — ${modeTitle(active.mode)}`}
                />
              )}
            </div>
          </>
        )}
      </section>
    </EntityList>
  );
}

function findNode(nodes: TreeNode[], rel: string): TreeNode | null {
  for (const n of nodes) {
    if (n.rel === rel) return n;
    if (n.isDir && rel.startsWith(n.rel + "/")) return findNode(n.children, rel);
  }
  return null;
}
