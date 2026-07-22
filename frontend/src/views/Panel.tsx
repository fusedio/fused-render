// Panel mode (M5, SPEC §14 / DECISIONS D45): a split-pane grid of /embed
// iframes whose whole arrangement + per-pane locations live in the reserved
// `_layout` URL param, so a panel layout is bookmarkable/refreshable like any
// view.
//
// The pane tree is a MUTABLE structure held in a ref (like the vanilla
// module's `tree` variable): pane URL syncs mutate leaf path/query in place
// (no re-render — the iframe must not be touched), while structural ops
// (split/close) bump a version counter to re-render the grid. Pane iframes
// freeze their src at mount: React must never write the src attribute again,
// or the pane would reload on every re-render (the vanilla shell set src once
// at creation; crumb clicks write it imperatively).
import { useEffect, useRef, useState, type ReactNode } from "react";
import { navigateUrl, urlForFsPath, IS_EMBED } from "../lib/router";
import {
  leaf,
  encodeNode,
  parseLayout,
  buildSentinelUrl,
  splitShellSearch,
  embedSrc,
  readEmbedLoc,
  attachEmbedUrlChange,
  detachEmbedUrlChange,
  type LayoutNode,
  type LayoutLeaf,
  type LayoutSplit,
  type UrlChangeHook,
  type EmbedLoc,
} from "../lib/layout-codec";
import type { Config } from "../lib/api";
import { ShareIcon } from "../components/ShareIcon";
import { SplitRightIcon, SplitDownIcon } from "../components/SplitIcons";
import PaneModeMenu from "../components/PaneModeMenu";

// Panel mode lives under the page's own prefix (`/view/_panel` or
// `/embed/_panel`), so entering/refreshing/exiting stays in the active mode.
const PANEL_PATH = (IS_EMBED ? "/embed/" : "/view/") + "_panel";

// Build <prefix>/_panel?... : the encoded tree plus any top-level params
// (hand-typed globals only, D72 — the shell never promotes params there).
// Exported for the breadcrumb's Split button (same acyclic exception as the
// vanilla breadcrumb.js -> views/panel.js import).
export function panelUrl(codecStr: string, merged?: Iterable<[string, string]> | null): string {
  return buildSentinelUrl(PANEL_PATH, codecStr, merged);
}

// Split-node keys for React reconciliation: leaves carry codec ids; splits
// get their own monotonic ids on creation/parse (a stable key keeps an
// untouched subtree's iframes alive across structural re-renders).
let splitSeq = 0;
function ensureIds(node: LayoutNode): LayoutNode {
  if (node.type === "split") {
    if (!node.id) node.id = ++splitSeq;
    node.children.forEach(ensureIds);
  }
  return node;
}
function nodeKey(node: LayoutNode): string {
  return (node.type === "split" ? "s" : "l") + node.id;
}

// --- Tree ops (verbatim from the vanilla module) ----------------------------
// findParent returns: the parent split, null when target IS the root, or
// false when target is not in this subtree.
function findParent(
  node: LayoutNode,
  target: LayoutNode,
  parent?: LayoutSplit,
): LayoutSplit | null | false {
  if (node === target) return parent === undefined ? null : parent;
  if (node.type === "split") {
    for (const c of node.children) {
      const r = findParent(c, target, node);
      if (r !== false) return r;
    }
  }
  return false;
}

function findLeaf(node: LayoutNode, id: number): LayoutLeaf | null {
  if (node.type === "leaf") return node.id === id ? node : null;
  for (const c of node.children) {
    const r = findLeaf(c, id);
    if (r) return r;
  }
  return null;
}

const ICONS = {
  splitRight: <SplitRightIcon />,
  splitDown: <SplitDownIcon />,
  max: (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path
        d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9.75 6.25M2.5 13.5l3.75-3.75"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  restore: (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path
        d="M6.5 5.5h-4v-4M9.5 10.5h4v4M2.5 5.5l3.5-3.5M13.5 10.5L10 14"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  ),
  close: (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
      <path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" strokeLinecap="round" />
    </svg>
  ),
};

interface PaneCtx {
  syncUrl: () => void;
  split: (id: number, dir: "row" | "col") => void;
  close: (id: number) => void;
  home: string;
}

// One pane: bar (crumbs + split/maximize/close buttons) over a frozen-src
// /embed iframe. Crumbs track the pane's LIVE location (loc state); the leaf
// node is mutated in place so `_layout` re-encoding sees it without a
// re-render.
function Pane({ node, ctx }: { node: LayoutLeaf; ctx: PaneCtx }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const hookRef = useRef<UrlChangeHook | null>(null);
  // Frozen at mount — a structural remount re-freezes from the (synced) node.
  const srcRef = useRef<string | null>(null);
  if (srcRef.current === null) srcRef.current = embedSrc(node.path, node.query);
  const [loc, setLoc] = useState<EmbedLoc>({ path: node.path, query: node.query });
  const [maximized, setMaximized] = useState(false);
  const crumbsRef = useRef<HTMLDivElement | null>(null);

  // On each load: sync this leaf from the pane's live location, re-encode the
  // `_layout` URL, redraw crumbs, and (re)attach the fused:urlchange listener
  // to the pane window — the embed shell dispatches it on client-side (SPA)
  // navigation that fires no iframe `load` (LM-6/LM-8).
  const onUrlChange = () => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const live = readEmbedLoc(iframe);
    if (live) {
      node.path = live.path;
      node.query = live.query;
      ctx.syncUrl();
      setLoc(live);
    }
  };

  const onLoad = () => {
    onUrlChange();
    // null when this document is already hooked — keep the existing hook.
    if (!iframeRef.current) return;
    const hook = attachEmbedUrlChange(iframeRef.current, onUrlChange);
    if (hook) hookRef.current = hook;
  };

  // Detach the pane's fused:urlchange listener on unmount (the vanilla
  // stopPanel()) so nothing fires after navigating away or closing the pane.
  useEffect(() => () => detachEmbedUrlChange(hookRef.current), []);

  // Keep the tail of a long path visible, like the vanilla renderCrumbs.
  useEffect(() => {
    const el = crumbsRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [loc]);

  // Same "~" contraction as the top-bar Breadcrumb: strictly below home only —
  // home itself shows its full path, not a lone "~".
  const underHome = loc.path.startsWith(ctx.home + "/");
  const parts = (underHome ? loc.path.slice(ctx.home.length) : loc.path)
    .split("/")
    .filter((s) => s.length > 0);
  const crumbs: ReactNode[] = [];
  const addCrumb = (label: string, targetPath: string, isLast: boolean, key: string) => {
    crumbs.push(
      <span
        key={key}
        className={"panel-crumb" + (isLast ? " last" : "")}
        onClick={() => {
          // Clicking a crumb navigates the pane's iframe to that prefix
          // (drops the pane-local query — a fresh location). Imperative src
          // write, exactly like vanilla — no React re-render involved.
          if (iframeRef.current) iframeRef.current.src = embedSrc(targetPath, "");
        }}
      >
        {label}
      </span>,
    );
  };
  addCrumb(underHome ? "~" : "/", underHome ? ctx.home : "/", parts.length === 0, "root");
  let acc = underHome ? ctx.home : "";
  parts.forEach((p, i) => {
    acc += "/" + p;
    // The "~" crumb carries no slash, so its first segment needs one too.
    if (i > 0 || underHome)
      crumbs.push(
        <span key={"sep" + i} className="panel-crumb-sep">
          /
        </span>,
      );
    addCrumb(p, acc, i === parts.length - 1, acc);
  });

  return (
    <div className={"panel-pane" + (maximized ? " maximized" : "")}>
      <div className="panel-bar">
        <div className="panel-crumbs" ref={crumbsRef}>
          {crumbs}
          {/* Inside the crumbs container so it hugs the last crumb instead of
              being pushed to the bar's right edge by the flex:1 crumbs. */}
          <button
            className="panel-btn open-plain"
            title="Open in a new tab"
            onClick={() => window.open(urlForFsPath(loc.path, loc.query), "_blank")}
          >
            <ShareIcon size={14} />
          </button>
        </div>
        {/* Template-mode menu for the pane's live location. Mode switch is an
            imperative src write (same as crumb clicks); onLoad then re-syncs
            the leaf + `_layout` from the reloaded pane. */}
        <PaneModeMenu
          path={loc.path}
          query={loc.query}
          onNavigate={(q) => {
            if (iframeRef.current) iframeRef.current.src = embedSrc(loc.path, q);
          }}
        />
        <button
          className="panel-btn split-right"
          title="Split right"
          onClick={() => ctx.split(node.id, "row")}
        >
          {ICONS.splitRight}
        </button>
        <button
          className="panel-btn split-down"
          title="Split down"
          onClick={() => ctx.split(node.id, "col")}
        >
          {ICONS.splitDown}
        </button>
        <button
          className="panel-btn maximize"
          title={maximized ? "Restore" : "Maximize"}
          onClick={() => setMaximized((m) => !m)}
        >
          {maximized ? ICONS.restore : ICONS.max}
        </button>
        <button className="panel-btn close" title="Close pane" onClick={() => ctx.close(node.id)}>
          {ICONS.close}
        </button>
      </div>
      <iframe ref={iframeRef} src={srcRef.current} onLoad={onLoad} />
    </div>
  );
}

function Build({ node, ctx }: { node: LayoutNode; ctx: PaneCtx }) {
  if (node.type === "split") {
    return (
      <div className={"panel-split " + node.dir}>
        {node.children.map((c) => (
          <Build key={nodeKey(c)} node={c} ctx={ctx} />
        ))}
      </div>
    );
  }
  return <Pane node={node} ctx={ctx} />;
}

export default function Panel({ config }: { config: Config }) {
  // Mark this window a param boundary BEFORE any iframe mounts (LM-3/D72,
  // same contract as tab mode): a page rendered inside a pane targets its own
  // embed URL, so params stay pane-local (captured segment-local in `_layout`
  // by syncUrl). Cleared on unmount — this shell window survives SPA
  // navigation to a normal view, and a stale flag would corrupt that view.
  window._fusedParamBoundary = true;
  useEffect(
    () => () => {
      delete window._fusedParamBoundary;
    },
    [],
  );

  // Build the pane tree from `_layout` on the shell URL once per mount (App
  // remounts Panel on every navigation). Missing/empty/unparseable `_layout`
  // falls back to a single pane of the start dir.
  const treeRef = useRef<LayoutNode | null>(null);
  if (treeRef.current === null) {
    const raw = splitShellSearch(location.search).layout;
    let tree: LayoutNode | null = null;
    if (raw && raw.trim()) {
      try {
        tree = parseLayout(raw);
      } catch {
        tree = null;
      }
    }
    treeRef.current = ensureIds(tree || leaf(config.start_dir, ""));
  }
  const [version, setVersion] = useState(0);

  // Re-encode `_layout` on the shell URL, passing hand-typed top-level params
  // through untouched (D72 — no user params are promoted here), and
  // replaceState only when the value actually changed (LM-6 guard). This
  // fires the shell's own fused:urlchange (main.tsx wraps replaceState), so
  // the bookmark buttons react.
  const syncUrl = () => {
    const { params } = splitShellSearch(location.search);
    const codecStr = encodeNode(treeRef.current!);
    const next = panelUrl(codecStr, params);
    if (location.pathname + location.search !== next) {
      history.replaceState(history.state, "", next);
    }
  };

  // Structural ops (split/close) get a real history entry: pushState the
  // re-encoded `_layout` at op time, so Back/Forward walk the arrangement
  // history. Plain history.pushState (not navigate()) — no NAV_EVENT, so the
  // grid doesn't remount now; on an actual popstate App's nav epoch remounts
  // Panel, which re-parses the entry's `_layout`. The version-effect syncUrl
  // that follows sees an unchanged URL and no-ops (LM-6 guard).
  const pushUrl = () => {
    const { params } = splitShellSearch(location.search);
    const next = panelUrl(encodeNode(treeRef.current!), params);
    if (location.pathname + location.search !== next) {
      history.pushState(history.state, "", next);
    }
  };

  const split = (id: number, dir: "row" | "col") => {
    const tree = treeRef.current!;
    const l = findLeaf(tree, id);
    if (!l) return;
    // The new pane duplicates the current pane's live location — the leaf is
    // already live-synced by the pane's onUrlChange, so it IS the location.
    const newLeaf = leaf(l.path, l.query);
    const parent = findParent(tree, l);
    if (parent && parent.dir === dir) {
      parent.children.splice(parent.children.indexOf(l) + 1, 0, newLeaf);
    } else {
      const splitNode = ensureIds({ type: "split", dir, children: [l, newLeaf] }) as LayoutSplit;
      if (!parent) treeRef.current = splitNode;
      else parent.children[parent.children.indexOf(l)] = splitNode;
    }
    pushUrl();
    setVersion((v) => v + 1);
  };

  const close = (id: number) => {
    const tree = treeRef.current!;
    const l = findLeaf(tree, id);
    if (!l) return;
    const parent = findParent(tree, l);
    if (!parent) {
      // Closing the last pane exits panel mode to a plain view of its
      // location (stays in the active prefix: view or embed).
      navigateUrl(urlForFsPath(l.path, l.query));
      return;
    }
    parent.children.splice(parent.children.indexOf(l), 1);
    if (parent.children.length === 1) {
      // Collapse the now single-child split into its child.
      const only = parent.children[0];
      const gp = findParent(tree, parent);
      if (!gp) treeRef.current = only;
      else gp.children[gp.children.indexOf(parent)] = only;
    }
    // A layout of one pane is pointless chrome — when the collapse leaves a
    // lone leaf at the root, exit panel mode to a plain view of it (same
    // semantics as closing the last pane above). Only close() can get here;
    // a hand-typed single-segment `_layout` still renders as a single pane.
    const root = treeRef.current!;
    if (root.type === "leaf") {
      navigateUrl(urlForFsPath(root.path, root.query));
      return;
    }
    pushUrl();
    setVersion((v) => v + 1);
  };

  // Sync after mount and after every structural change (the vanilla render()
  // called syncPanelUrl() after rebuilding the DOM).
  useEffect(() => {
    syncUrl();
  }, [version]);

  // Windows expanduser returns backslashes; pane paths are always forward-slash.
  const ctx: PaneCtx = { syncUrl, split, close, home: config.home.replace(/\\/g, "/") };
  return (
    <div className="panel-root">
      <Build node={treeRef.current} ctx={ctx} />
    </div>
  );
}
