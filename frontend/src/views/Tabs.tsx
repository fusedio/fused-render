// Tab mode (M6, SPEC §15 / DECISIONS D47/D48): a tabbed set of /embed iframes.
// Same URL-is-state model as panel mode but one page visible at a time. The
// tab list is a flat top-level `,` row of the shared `_layout` codec; the
// active tab is deliberately NOT encoded (TM-6) so switching never dirties the
// "Update bookmark" state. Params are tab-INDEPENDENT (TM-3, same contract as
// panel mode since D72): the tab shell marks its window `_fusedParamBoundary`
// so each tab's runtime targets its own /embed URL, and every tab's full
// query is captured segment-local inside `_layout`.
//
// Iframe discipline (TM-5): lazy-mounted on first activation, kept alive
// (display:none) thereafter, NEVER re-parented or re-ordered in the DOM (both
// reload an iframe). The frames render as a flat keyed list that only ever
// appends (new tab) or removes (close); src is frozen at mount and never
// written again by React.
import { useEffect, useRef, useState } from "react";
import { navigateUrl, urlForFsPath, IS_EMBED, VIEW_PREFIX } from "../lib/router";
import { basename } from "../lib/format";
import {
  leaf,
  encodePaneSegment,
  parseLayout,
  flattenToLeaves,
  buildSentinelUrl,
  splitShellSearch,
  embedSrc,
  readEmbedLoc,
  attachEmbedUrlChange,
  detachEmbedUrlChange,
  type LayoutLeaf,
  type UrlChangeHook,
} from "../lib/layout-codec";
import type { Config } from "../lib/api";
import type { Bookmark } from "../lib/bookmarks";
import { ShareIcon } from "../components/ShareIcon";

// Tab mode lives under the page's own prefix, like panel mode.
const TAB_PATH = (IS_EMBED ? "/embed/" : "/view/") + "_tab";

// Compose a `/view/_tab` URL from a folder's child bookmarks (TM-8, sidebar
// entry). Tab params are independent (TM-3/D47): each child's WHOLE saved
// query stays segment-local — no hoisting, no collision handling — so every
// tab reproduces its bookmark verbatim. Exported for
// Sidebar.tsx (documented acyclic exception, mirrors breadcrumb -> Panel).
export function composeFolderTabsUrl(children: Bookmark[]): string {
  const segments = children.map((b) => {
    const qIdx = b.url.indexOf("?");
    const pathname = qIdx === -1 ? b.url : b.url.slice(0, qIdx);
    let search = qIdx === -1 ? "" : b.url.slice(qIdx); // keeps its leading "?"
    // Carry the bookmark's NAME into the segment as reserved `_label` so the
    // tab bar shows it verbatim (tabLabel) — a renamed bookmark or a saved
    // `_panel` layout must not degrade to a basename/"Panel" label. Inserted
    // BEFORE any `_layout` span to keep the layout-last convention (D51);
    // `_`-prefix keeps it invisible to fused.params (PR-6).
    if (b.name) {
      // Split at the raw `_layout=(...)` span (kept byte-identical — it may
      // contain literal `&` and even nested `_label`s of its own segments);
      // the head is plain params, so URLSearchParams can REPLACE any `_label`
      // already saved in the bookmark URL (a stale one would win otherwise —
      // get() returns the first occurrence).
      const layoutIdx = search.indexOf("_layout=(");
      const head = layoutIdx === -1 ? search : search.slice(0, layoutIdx);
      const tail = layoutIdx === -1 ? "" : search.slice(layoutIdx);
      const params = new URLSearchParams(head.replace(/^\?/, "").replace(/&$/, ""));
      params.set("_label", b.name);
      const qs = params.toString();
      search = "?" + qs + (tail ? (qs ? "&" : "") + tail : "");
    }
    // Sentinel pathnames (/view/_panel, /view/_tab) decode to segment paths
    // "/_panel" / "/_tab" — round-trips through embedSrc/readEmbedLoc (TM-4).
    const fsPath = pathname.startsWith(VIEW_PREFIX)
      ? "/" +
        pathname
          .slice(VIEW_PREFIX.length)
          .split("/")
          .filter((s) => s.length > 0)
          .map(decodeURIComponent)
          .join("/")
      : pathname;
    return encodePaneSegment(fsPath, search);
  });
  return buildSentinelUrl(TAB_PATH, segments.join(","), null);
}

// Missing/empty/unparseable `_layout` → single tab of the start dir. Any
// nested `;`/`()` structure is defensively flattened to its leaves in
// document order (TM-2), each leaf a tab.
function parseTabs(raw: string | null, startDir: string): LayoutLeaf[] {
  if (raw && raw.trim()) {
    try {
      const leaves = flattenToLeaves(parseLayout(raw));
      if (leaves.length) return leaves;
    } catch {
      /* fall through to the start-dir fallback */
    }
  }
  return [leaf(startDir, "")];
}

// A reserved `_label` param on the tab's query wins (set by folder-as-tabs
// entry so tabs carry their BOOKMARK names, TM-8). It rides the segment query,
// so it survives refresh/bookmark and is dropped naturally when the tab
// navigates elsewhere (navigation replaces the embed query wholesale).
// Fallback: basename of the tab's live path (mutated into the leaf by the URL
// sync) — sentinel paths label as Panel / Tabs (TM-6).
function tabLabel(t: LayoutLeaf): string {
  const named = splitShellSearch(t.query || "").params.get("_label");
  if (named) return named;
  const b = basename(t.path);
  if (b === "_panel") return "Panel";
  if (b === "_tab") return "Tabs";
  return b;
}

// One keep-alive tab iframe. src frozen at mount; visibility via display so
// hidden tabs keep receiving fused:urlchange (the runtime listens on the top
// window, D46) and stay param-synced while hidden.
function TabFrame({ tab, active, onLocSync }: { tab: LayoutLeaf; active: boolean; onLocSync: () => void }) {
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  const hookRef = useRef<UrlChangeHook | null>(null);
  const srcRef = useRef<string | null>(null);
  if (srcRef.current === null) srcRef.current = embedSrc(tab.path, tab.query);

  // On each load: sync this tab from its live location, re-encode `_layout`,
  // refresh the label, and (re)attach the fused:urlchange listener — the
  // embed shell dispatches it on client-side navigation that fires no `load`
  // (TM-7).
  const onUrlChange = () => {
    const iframe = iframeRef.current;
    if (!iframe) return;
    const live = readEmbedLoc(iframe);
    if (live) {
      tab.path = live.path;
      tab.query = live.query;
      onLocSync();
    }
  };

  const onLoad = () => {
    onUrlChange();
    // null when this document is already hooked — keep the existing hook.
    if (!iframeRef.current) return;
    const hook = attachEmbedUrlChange(iframeRef.current, onUrlChange);
    if (hook) hookRef.current = hook;
  };

  useEffect(() => () => detachEmbedUrlChange(hookRef.current), []);

  return (
    <iframe
      ref={iframeRef}
      src={srcRef.current}
      onLoad={onLoad}
      style={active ? undefined : { display: "none" }}
    />
  );
}

export default function Tabs({ config }: { config: Config }) {
  // Mark this window a param boundary BEFORE any iframe mounts (TM-3): a page
  // rendered inside a tab must not climb past its own embed shell to here.
  // Render-time set matches the vanilla ordering (renderTabs set it before
  // building any DOM); cleared on unmount — this shell window survives SPA
  // navigation to a normal view, and a stale flag would corrupt that view.
  window._fusedParamBoundary = true;
  useEffect(
    () => () => {
      delete window._fusedParamBoundary;
    },
    []
  );

  const [tabs, setTabs] = useState<LayoutLeaf[]>(() =>
    parseTabs(splitShellSearch(location.search).layout, config.start_dir)
  );
  // `_layout` never encodes activation (TM-2 — URL shape untouched), so the
  // active tab rides on the history entry's state object instead: pushHistory
  // stamps `{fusedActiveTab: index}` and this remount (App re-keys Tabs on the
  // popstate nav epoch) reads it back, making Back/Forward restore which tab
  // was active. Fresh loads have no state → first tab, as before.
  const [activeId, setActiveId] = useState<number>(() => {
    const idx = (history.state as { fusedActiveTab?: unknown } | null)?.fusedActiveTab;
    return typeof idx === "number" && tabs[idx] ? tabs[idx].id : tabs[0].id;
  });
  // Lazy mount (TM-5): a tab's iframe exists only once it has been activated.
  const [mountedIds, setMountedIds] = useState<number[]>(() => [activeId]);
  // Leaf objects are mutated in place by the URL sync (path/query); this
  // counter re-renders the bar so labels track live locations.
  const [, bumpLabels] = useState(0);

  const tabsRef = useRef<LayoutLeaf[]>(tabs);
  tabsRef.current = tabs;

  // Tab list encodes as a flat comma row (TM-2). Re-encode `_layout`, passing
  // the remaining top-level params through (no user params live there in tab
  // mode, but hand-added keys must not be dropped), and replaceState only on
  // an actual change (TM-7 guard) — which fires the shell's own
  // fused:urlchange so the bookmark buttons react.
  const syncUrl = () => {
    const { params } = splitShellSearch(location.search);
    const codecStr = tabsRef.current.map((t) => encodePaneSegment(t.path, t.query)).join(",");
    const next = buildSentinelUrl(TAB_PATH, codecStr, params);
    if (location.pathname + location.search !== next) {
      history.replaceState(history.state, "", next);
    }
  };

  // Tab ops (add/switch/close) get a real history entry. Pushed even when the
  // URL is unchanged (a switch moves no URL bits — activation lives only in
  // the entry's state, see the activeId initializer): the entry carries
  // `{fusedActiveTab: index}` so Back/Forward restore the active tab. Index,
  // not id — leaf ids are per-mount and don't survive the popstate remount.
  // Plain history.pushState (not navigate()) — no NAV_EVENT, so no remount
  // now; main.tsx's wrapper still fires fused:urlchange for the chrome.
  const pushHistory = (nextActiveId: number) => {
    const { params } = splitShellSearch(location.search);
    const codecStr = tabsRef.current.map((t) => encodePaneSegment(t.path, t.query)).join(",");
    const next = buildSentinelUrl(TAB_PATH, codecStr, params);
    const idx = tabsRef.current.findIndex((t) => t.id === nextActiveId);
    history.pushState({ fusedActiveTab: idx === -1 ? 0 : idx }, "", next);
  };

  const onLocSync = () => {
    syncUrl();
    bumpLabels((n) => n + 1);
  };

  // Initial `_layout` normalization on mount (vanilla render() synced once).
  useEffect(() => {
    syncUrl();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const activate = (id: number) => {
    setActiveId(id);
    setMountedIds((ids) => (ids.includes(id) ? ids : [...ids, id]));
  };

  const addTab = () => {
    const t = leaf(config.start_dir, "");
    setTabs((ts) => [...ts, t]);
    activate(t.id);
    // pushHistory reads tabsRef — update it eagerly so the push sees the new tab.
    tabsRef.current = [...tabsRef.current, t];
    pushHistory(t.id);
  };

  const closeTab = (id: number) => {
    const ts = tabsRef.current;
    const idx = ts.findIndex((t) => t.id === id);
    if (idx === -1) return;
    const t = ts[idx];
    if (ts.length === 1) {
      // Closing the last tab exits tab mode to a plain view of its live
      // location (stays in the active prefix), mirroring panel's last-pane
      // close. The leaf is live-synced, so it IS the location.
      navigateUrl(urlForFsPath(t.path, t.query));
      return;
    }
    const next = ts.filter((x) => x.id !== id);
    tabsRef.current = next;
    setTabs(next);
    setMountedIds((ids) => ids.filter((x) => x !== id));
    let nextActive = activeId;
    if (activeId === id) {
      // Activate a neighbor (prefer the one now at this slot, else the last).
      nextActive = next[Math.min(idx, next.length - 1)].id;
      activate(nextActive);
    }
    pushHistory(nextActive);
  };

  return (
    <div className="tabs-root">
      <div className="tabs-bar">
        {tabs.map((t) => (
          <button
            key={t.id}
            className={"tab" + (t.id === activeId ? " active" : "")}
            onClick={() => {
              // Re-clicking the active tab is a no-op — no history entry.
              if (t.id === activeId) return;
              activate(t.id);
              pushHistory(t.id);
            }}
          >
            <span className="tab-label">{tabLabel(t)}</span>
            <span
              className="tab-open-plain"
              title="Open in a new tab"
              onClick={(e) => {
                // Open this tab's live location as a plain (non-layout) view
                // in a new browser tab; don't also activate the tab.
                e.stopPropagation();
                window.open(urlForFsPath(t.path, t.query), "_blank");
              }}
            >
              <ShareIcon size={12} />
            </span>
            <span
              className="tab-close"
              title="Close tab"
              onClick={(e) => {
                e.stopPropagation();
                closeTab(t.id);
              }}
            >
              ×
            </span>
          </button>
        ))}
        <button className="tab-add" title="New tab" onClick={addTab}>
          +
        </button>
      </div>
      <div className="tabs-body">
        {/* Flat keyed list in tabs order; the list only appends/removes, never
            reorders, so React never moves (= reloads) a live iframe. */}
        {tabs
          .filter((t) => mountedIds.includes(t.id))
          .map((t) => (
            <TabFrame key={t.id} tab={t} active={t.id === activeId} onLocSync={onLocSync} />
          ))}
      </div>
    </div>
  );
}
