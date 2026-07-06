// Panel mode (M5, SPEC §14 / DECISIONS D45): a split-pane grid of /embed
// iframes whose whole arrangement + per-pane locations live in the reserved
// `_layout` URL param, so a panel layout is bookmarkable/refreshable like any
// view. Imports router.js and the shared layout-codec.js only (one-way deps,
// ARCHITECTURE §6).
import { navigateUrl, urlForFsPath, IS_EMBED } from "../router.js";
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
} from "./layout-codec.js";

// Panel mode lives under the page's own prefix (`/view/_panel` or
// `/embed/_panel`), so entering/refreshing/exiting stays in the active mode.
const PANEL_PATH = (IS_EMBED ? "/embed/" : "/view/") + "_panel";

const contentEl = document.getElementById("content");

// Build <prefix>/_panel?... : the encoded tree plus the merged (top-level)
// param pool. Exported for breadcrumb.js's Split button.
export function panelUrl(codecStr, merged) {
  return buildSentinelUrl(PANEL_PATH, codecStr, merged);
}

// Re-encode `_layout` on the shell URL, preserving the merged pool, and
// replaceState only when the value actually changed (LM-6 guard). This fires
// the shell's own fused:urlchange (main.js wraps replaceState), so the
// bookmark buttons react.
function syncPanelUrl() {
  const { params } = splitShellSearch(location.search);
  const codecStr = encodeNode(tree);
  const next = panelUrl(codecStr, params);
  if (location.pathname + location.search !== next) {
    history.replaceState(history.state, "", next);
  }
}

// Read a pane's live location from its same-origin iframe (D39): fs path +
// query, so duplicates/crumbs/sync follow in-pane navigation.
function readPaneLoc(pane) {
  return readEmbedLoc(pane.querySelector("iframe"));
}

// --- Tree ops (borrowed from the reference grid-viewer) --------------------
function findParent(node, target, parent) {
  if (node === target) return parent === undefined ? null : parent;
  if (node.type === "split") {
    for (const c of node.children) {
      const r = findParent(c, target, node);
      if (r !== false) return r;
    }
  }
  return false;
}

function findLeaf(node, id) {
  if (node.type === "leaf") return node.id === id ? node : null;
  for (const c of node.children) {
    const r = findLeaf(c, id);
    if (r) return r;
  }
  return null;
}

function splitLeaf(id, dir) {
  const l = findLeaf(tree, id);
  if (!l) return;
  // The new pane duplicates the current pane's live location.
  const loc = readPaneLocById(id);
  const newLeaf = loc ? leaf(loc.path, loc.query) : leaf(l.path, l.query);
  const parent = findParent(tree, l);
  if (parent && parent.dir === dir) {
    parent.children.splice(parent.children.indexOf(l) + 1, 0, newLeaf);
  } else {
    const split = { type: "split", dir, children: [l, newLeaf] };
    if (!parent) tree = split;
    else parent.children[parent.children.indexOf(l)] = split;
  }
  render();
}

function closeLeaf(id) {
  const l = findLeaf(tree, id);
  if (!l) return;
  const parent = findParent(tree, l);
  if (!parent) {
    // Closing the last pane exits panel mode to a plain view of its location
    // (stays in the active prefix: view or embed).
    const loc = readPaneLocById(id) || { path: l.path, query: l.query };
    navigateUrl(urlForFsPath(loc.path, loc.query));
    return;
  }
  parent.children.splice(parent.children.indexOf(l), 1);
  if (parent.children.length === 1) {
    // Collapse the now single-child split into its child.
    const only = parent.children[0];
    const gp = findParent(tree, parent);
    if (!gp) tree = only;
    else gp.children[gp.children.indexOf(parent)] = only;
  }
  render();
}

function readPaneLocById(id) {
  const pane = contentEl.querySelector(`.panel-pane[data-id="${id}"]`);
  return pane ? readPaneLoc(pane) : null;
}

// --- Rendering -------------------------------------------------------------
let tree = null;
let panelRootEl = null;

const ICONS = {
  splitRight: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" stroke="currentColor"/><path d="M8 2.5h5a1.5 1.5 0 0 1 1.5 1.5v8a1.5 1.5 0 0 1-1.5 1.5H8z" fill="currentColor"/></svg>`,
  splitDown: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><rect x="1.5" y="2.5" width="13" height="11" rx="1.5" stroke="currentColor"/><path d="M1.5 8h13v4a1.5 1.5 0 0 1-1.5 1.5H3A1.5 1.5 0 0 1 1.5 12z" fill="currentColor"/></svg>`,
  max: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M9.5 2.5h4v4M6.5 13.5h-4v-4M13.5 2.5L9.75 6.25M2.5 13.5l3.75-3.75" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  restore: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M6.5 5.5h-4v-4M9.5 10.5h4v4M2.5 5.5l3.5-3.5M13.5 10.5L10 14" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  close: `<svg width="14" height="14" viewBox="0 0 16 16" fill="none"><path d="M4 4l8 8M12 4l-8 8" stroke="currentColor" stroke-linecap="round"/></svg>`,
};

function render() {
  contentEl.innerHTML = "";
  panelRootEl = document.createElement("div");
  panelRootEl.className = "panel-root";
  panelRootEl.appendChild(build(tree));
  contentEl.appendChild(panelRootEl);
  syncPanelUrl();
}

function build(node) {
  if (node.type === "split") {
    const el = document.createElement("div");
    el.className = "panel-split " + node.dir;
    node.children.forEach((c) => el.appendChild(build(c)));
    return el;
  }
  return buildPane(node);
}

// Bar button: class + SVG icon + title. Icon strings are trusted constants, so
// innerHTML stays confined to them.
function barBtn(cls, icon, title) {
  const btn = document.createElement("button");
  btn.className = "panel-btn " + cls;
  btn.title = title;
  btn.innerHTML = icon;
  return btn;
}

function buildPane(node) {
  const pane = document.createElement("div");
  pane.className = "panel-pane";
  pane.dataset.id = node.id;

  const bar = document.createElement("div");
  bar.className = "panel-bar";
  const crumbs = document.createElement("div");
  crumbs.className = "panel-crumbs";
  const splitRight = barBtn("split-right", ICONS.splitRight, "Split right");
  const splitDown = barBtn("split-down", ICONS.splitDown, "Split down");
  const maximize = barBtn("maximize", ICONS.max, "Maximize");
  const close = barBtn("close", ICONS.close, "Close pane");
  bar.append(crumbs, splitRight, splitDown, maximize, close);

  const iframe = document.createElement("iframe");
  iframe.src = embedSrc(node.path, node.query);
  pane.append(bar, iframe);

  // On each load: sync this leaf from the pane's live location, re-encode the
  // `_layout` URL, redraw crumbs, and (re)attach the fused:urlchange listener
  // to the pane window — the embed shell dispatches it on client-side (SPA)
  // navigation that fires no iframe `load` (LM-6/LM-8).
  const onUrlChange = () => {
    const loc = readPaneLoc(pane);
    if (loc) {
      node.path = loc.path;
      node.query = loc.query;
      syncPanelUrl();
    }
    renderCrumbs(pane, node);
  };
  iframe.addEventListener("load", () => {
    onUrlChange();
    // null when this document is already hooked — keep the existing hook.
    const hook = attachEmbedUrlChange(iframe, onUrlChange);
    if (hook) pane._hook = hook;
  });
  renderCrumbs(pane, node);

  splitRight.onclick = () => splitLeaf(node.id, "row");
  splitDown.onclick = () => splitLeaf(node.id, "col");
  close.onclick = () => closeLeaf(node.id);
  maximize.onclick = () => {
    const on = pane.classList.toggle("maximized");
    maximize.innerHTML = on ? ICONS.restore : ICONS.max;
    maximize.title = on ? "Restore" : "Maximize";
  };
  return pane;
}

function renderCrumbs(pane, node) {
  const el = pane.querySelector(".panel-crumbs");
  el.innerHTML = "";
  const iframe = pane.querySelector("iframe");
  const parts = node.path.split("/").filter((s) => s.length > 0);
  const addCrumb = (label, targetPath, isLast) => {
    const c = document.createElement("span");
    c.className = "panel-crumb" + (isLast ? " last" : "");
    c.textContent = label;
    // Clicking a crumb navigates the pane's iframe to that prefix (drops the
    // pane-local query — a fresh location).
    c.onclick = () => {
      iframe.src = embedSrc(targetPath, "");
    };
    el.appendChild(c);
  };
  addCrumb("/", "/", parts.length === 0);
  let acc = "";
  parts.forEach((p, i) => {
    acc += "/" + p;
    if (i > 0) {
      const sep = document.createElement("span");
      sep.className = "panel-crumb-sep";
      sep.textContent = "/";
      el.appendChild(sep);
    }
    addCrumb(p, acc, i === parts.length - 1);
  });
  el.scrollLeft = el.scrollWidth;
}

// --- Public API ------------------------------------------------------------
// Build the pane tree from `_layout` on the shell URL and render it. Missing/
// empty/unparseable `_layout` falls back to a single pane of the start dir.
export function renderPanel(config) {
  const raw = splitShellSearch(location.search).layout;
  if (raw && raw.trim()) {
    try {
      tree = parseLayout(raw);
    } catch (e) {
      tree = leaf(config.start_dir, "");
    }
  } else {
    tree = leaf(config.start_dir, "");
  }
  render();
}

// Tear down (parallels stopListingWatch): detach pane fused:urlchange
// listeners so nothing fires after we navigate away from panel mode.
export function stopPanel() {
  if (!panelRootEl) return;
  panelRootEl.querySelectorAll(".panel-pane").forEach((pane) => {
    detachEmbedUrlChange(pane._hook);
    pane._hook = null;
  });
  panelRootEl = null;
  tree = null;
}
