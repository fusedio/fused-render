// Tab mode (M6, SPEC §15 / DECISIONS D47/D48): a tabbed set of /embed iframes.
// Same URL-is-state model as layout mode (§14) but one page visible at a time.
// The tab list is a flat top-level `,` row of the shared `_layout` codec; the
// active tab is deliberately NOT encoded (TM-6) so switching never dirties the
// "Update bookmark" state. Params are tab-INDEPENDENT (TM-3, inverting panel's
// merged pool): the tab shell marks its window `_fusedParamBoundary` so each
// tab's runtime targets its own /embed URL, and every tab's full query is
// captured segment-local inside `_layout`. A nested _panel still merges among
// its own panes (its climb halts at the panel shell, below this boundary).
// Imports router.js/format.js and the shared codec (TM-10) — the one-way dep
// rule holds; sidebar.js imports composeFolderTabsUrl here (the same acyclic
// exception breadcrumb.js has on views/panel.js).
import { navigateUrl, urlForFsPath, IS_EMBED, VIEW_PREFIX } from "../router.js";
import { basename } from "../format.js";
import {
  leaf,
  encodePaneSegment,
  parseLayout,
  flattenToLeaves,
  buildSentinelUrl,
  embedSrc,
  readEmbedLoc,
  attachEmbedUrlChange,
  detachEmbedUrlChange,
} from "./layout-codec.js";

// Tab mode lives under the page's own prefix, like layout mode.
const TAB_PATH = (IS_EMBED ? "/embed/" : "/view/") + "_tab";

const contentEl = document.getElementById("content");

let config = null;
let tabs = []; // leaf nodes; each grows a live `._iframe` on first activation
let activeId = null;
let tabsRootEl = null;
let barEl = null;
let bodyEl = null;

// --- `_layout` <-> shell URL ----------------------------------------------
// Tab list encodes as a flat comma row (TM-2). Re-encode `_layout`, passing
// the remaining top-level params through (no user params live there in tab
// mode, but hand-added keys must not be dropped), and replaceState only on an
// actual change (TM-7 guard) —
// which fires the shell's own fused:urlchange (main.js wraps replaceState) so
// the bookmark buttons react.
function syncTabsUrl() {
  const current = new URLSearchParams(location.search);
  const codecStr = tabs.map((t) => encodePaneSegment(t.path, t.query)).join(",");
  const next = buildSentinelUrl(TAB_PATH, codecStr, current);
  if (location.pathname + location.search !== next) {
    history.replaceState(history.state, "", next);
  }
}

// Missing/empty/unparseable `_layout` → single tab of the start dir. Any nested
// `;`/`()` structure is defensively flattened to its leaves in document order
// (TM-2), each leaf a tab.
function parseTabs(raw) {
  if (raw && raw.trim()) {
    try {
      const leaves = flattenToLeaves(parseLayout(raw));
      if (leaves.length) return leaves;
    } catch (e) {
      /* fall through to the start-dir fallback */
    }
  }
  return [leaf(config.start_dir, "")];
}

// --- Labels ----------------------------------------------------------------
// Basename of the tab's live path (once mounted) or its segment path; sentinel
// paths label as Panel / Tabs (TM-6).
function tabLabel(t) {
  let path = t.path;
  if (t._iframe) {
    const loc = readEmbedLoc(t._iframe);
    if (loc) path = loc.path;
  }
  const b = basename(path);
  if (b === "_panel") return "Panel";
  if (b === "_tab") return "Tabs";
  return b;
}

function updateLabel(t) {
  if (!barEl) return;
  const el = barEl.querySelector(`.tab[data-id="${CSS.escape(String(t.id))}"] .tab-label`);
  if (el) el.textContent = tabLabel(t);
}

// --- Rendering -------------------------------------------------------------
// The body container is built once and its iframes are never re-parented (that
// would reload them). Only the bar is rebuilt on structural changes; iframes
// are appended on first activation and removed only on close.
function render() {
  contentEl.innerHTML = "";
  tabsRootEl = document.createElement("div");
  tabsRootEl.className = "tabs-root";
  barEl = document.createElement("div");
  barEl.className = "tabs-bar";
  bodyEl = document.createElement("div");
  bodyEl.className = "tabs-body";
  tabsRootEl.append(barEl, bodyEl);
  contentEl.appendChild(tabsRootEl);
  activate(activeId); // mounts + shows the active tab and builds the bar
  syncTabsUrl();
}

function renderBar() {
  barEl.innerHTML = "";
  tabs.forEach((t) => {
    const btn = document.createElement("button");
    btn.className = "tab" + (t.id === activeId ? " active" : "");
    btn.dataset.id = t.id;
    const label = document.createElement("span");
    label.className = "tab-label";
    label.textContent = tabLabel(t);
    const close = document.createElement("span");
    close.className = "tab-close";
    close.title = "Close tab";
    close.textContent = "×"; // ×
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      closeTab(t.id);
    });
    btn.append(label, close);
    btn.addEventListener("click", () => activate(t.id));
    barEl.appendChild(btn);
  });
  const add = document.createElement("button");
  add.className = "tab-add";
  add.title = "New tab";
  add.textContent = "+";
  add.addEventListener("click", addTab);
  barEl.appendChild(add);
}

// Create a tab's iframe the first time it is activated (lazy mount, TM-5) and
// keep it alive (display toggled) thereafter.
function mountTab(t) {
  if (t._iframe) return;
  const iframe = document.createElement("iframe");
  iframe.src = embedSrc(t.path, t.query);
  iframe.style.display = "none"; // activate() reveals the active one
  // On each load: sync this tab from its live location, re-encode `_layout`,
  // refresh the label, and (re)attach the fused:urlchange listener — the embed
  // shell dispatches it on client-side navigation that fires no `load` (TM-7).
  const onUrlChange = () => {
    const loc = readEmbedLoc(iframe);
    if (loc) {
      t.path = loc.path;
      t.query = loc.query;
      syncTabsUrl();
    }
    updateLabel(t);
  };
  iframe.addEventListener("load", () => {
    onUrlChange();
    // null when this document is already hooked — keep the existing hook.
    const hook = attachEmbedUrlChange(iframe, onUrlChange);
    if (hook) t._hook = hook;
  });
  t._iframe = iframe;
  bodyEl.appendChild(iframe);
}

function activate(id) {
  const t = tabs.find((x) => x.id === id);
  if (!t) return;
  activeId = id;
  mountTab(t);
  tabs.forEach((x) => {
    if (x._iframe) x._iframe.style.display = x.id === activeId ? "" : "none";
  });
  renderBar();
}

function addTab() {
  const t = leaf(config.start_dir, "");
  tabs.push(t);
  activate(t.id);
  syncTabsUrl();
}

function closeTab(id) {
  const idx = tabs.findIndex((t) => t.id === id);
  if (idx === -1) return;
  const t = tabs[idx];
  if (tabs.length === 1) {
    // Closing the last tab exits tab mode to a plain view of its live location
    // (stays in the active prefix), mirroring panel's last-pane close.
    const loc = (t._iframe && readEmbedLoc(t._iframe)) || { path: t.path, query: t.query };
    navigateUrl(urlForFsPath(loc.path, loc.query));
    return;
  }
  detachEmbedUrlChange(t._hook);
  t._hook = null;
  if (t._iframe) t._iframe.remove();
  tabs.splice(idx, 1);
  if (activeId === id) {
    // Activate a neighbor (prefer the one now at this slot, else the last).
    activeId = tabs[Math.min(idx, tabs.length - 1)].id;
  }
  activate(activeId);
  syncTabsUrl();
}

// --- Public API ------------------------------------------------------------
// Build the tab list from `_layout` and render. First tab active by default.
export function renderTabs(cfg) {
  config = cfg;
  // Mark this window a param boundary BEFORE any iframe mounts (TM-3): a page
  // rendered inside a tab must not climb past its own embed shell to here.
  window._fusedParamBoundary = true;
  tabs = parseTabs(new URLSearchParams(location.search).get("_layout"));
  activeId = tabs[0].id;
  render();
}

// Tear down (parallels stopPanel): detach tab fused:urlchange listeners so
// nothing fires after we navigate away. main.js calls this at route() top.
export function stopTabs() {
  // Clear the boundary even if never fully rendered: this shell window survives
  // SPA navigation to a normal view, and a stale flag would make that view's
  // rendered pages target themselves instead of the top window (TM-3).
  delete window._fusedParamBoundary;
  if (!tabsRootEl) return;
  tabs.forEach((t) => {
    detachEmbedUrlChange(t._hook);
    t._hook = null;
  });
  tabsRootEl = null;
  barEl = null;
  bodyEl = null;
  tabs = [];
  activeId = null;
}

// --- Folder → tabs composition (TM-8, sidebar entry) -----------------------
// Compose a `/view/_tab` URL from a folder's child bookmarks. Tab params are
// independent (TM-3/D47): each child's WHOLE saved query stays segment-local —
// no hoisting, no merged pool, no collision handling — so every tab reproduces
// its bookmark verbatim. Exported for sidebar.js (documented acyclic exception,
// mirrors breadcrumb -> views/panel.js).
export function composeFolderTabsUrl(children) {
  const segments = children.map((b) => {
    const qIdx = b.url.indexOf("?");
    const pathname = qIdx === -1 ? b.url : b.url.slice(0, qIdx);
    const search = qIdx === -1 ? "" : b.url.slice(qIdx); // keeps its leading "?"
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
