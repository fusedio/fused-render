// Crumb bar + "+ Bookmark" / "Split" buttons. Rendered by every view.
import { navigate, navigateUrl, currentUrl, IS_EMBED } from "./router.js";
import { escapeHtml, basename } from "./format.js";
import { addBookmark, allBookmarks, updateBookmarkUrl, armBookmark, disarmBookmark, getArmedBookmark } from "./bookmarks.js";
import { renderSidebar, syncStarButton } from "./sidebar.js";
import { encodePaneSegment, layoutUrl } from "./views/panel.js";

const breadcrumbEl = document.getElementById("breadcrumb");

// Shared bookmark/update button block (present on every view). `includeSplit`
// adds the layout-mode entry point; layout mode itself hides it.
function actionsHtml(includeSplit) {
  return `
    <div class="crumb-actions">
      <button id="update-bookmark-btn" class="star-btn starred" title="Update bookmark to current params" style="display:none">Update bookmark</button>
      ${includeSplit ? `<button id="split-btn" class="star-btn" title="Open this view in layout mode">Split</button>` : ""}
      <button id="bookmark-btn" class="star-btn" title="Bookmark this view">+ Bookmark</button>
    </div>`;
}

// Wire the bookmark + update buttons. `name` is the default bookmark name.
function wireActions(name) {
  document.getElementById("bookmark-btn").addEventListener("click", () => {
    addBookmark(name, currentUrl());
    renderSidebar();
  });
  document.getElementById("update-bookmark-btn").addEventListener("click", () => {
    const armed = getArmedBookmark();
    if (!armed) return;
    const url = currentUrl();
    updateBookmarkUrl(armed.id, url);
    armBookmark(armed.id, url); // re-arm against the newly saved url
    renderSidebar();
    syncUpdateButton();
  });
  syncStarButton();
  syncUpdateButton();
}

export function renderBreadcrumb(fsPath) {
  const parts = fsPath.split("/").filter((s) => s.length > 0);
  let acc = "";
  const pieces = [`<a href="#" data-path="/">/</a>`];
  parts.forEach((part, i) => {
    acc += "/" + part;
    const isLast = i === parts.length - 1;
    pieces.push(`<span class="sep">/</span>`);
    if (isLast) {
      pieces.push(`<span class="current">${escapeHtml(part)}</span>`);
    } else {
      pieces.push(`<a href="#" data-path="${escapeHtml(acc)}">${escapeHtml(part)}</a>`);
    }
  });
  breadcrumbEl.innerHTML = `
    <div class="crumbs">${pieces.join("")}</div>
    ${actionsHtml(true)}`;
  breadcrumbEl.querySelectorAll("a[data-path]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(a.getAttribute("data-path"));
    });
  });
  document.getElementById("split-btn").addEventListener("click", () => enterLayout(fsPath));
  wireActions(basename(fsPath));
}

// Layout-mode breadcrumb (LM-11): a static "Layout" label, no Split button.
// ★ Bookmark and Update-bookmark still work — they operate on currentUrl(),
// which is the layout URL, so bookmarking a layout needs zero bookmark-layer
// changes (D20/D38).
export function renderLayoutBreadcrumb() {
  breadcrumbEl.innerHTML = `
    <div class="crumbs"><span class="current">Layout</span></div>
    ${actionsHtml(false)}`;
  wireActions("Layout");
}

// Split entry (LM-10): the current view becomes the first pane. Its `_`-prefixed
// params and the listing sort/order stay pane-local (carried inside the
// `_layout` segment); every other param joins the merged top-level pool shared
// by all panes (LM-3).
function enterLayout(fsPath) {
  const params = new URLSearchParams(location.search);
  const paneLocal = new URLSearchParams();
  const merged = [];
  for (const [k, v] of params) {
    if (k.startsWith("_") || k === "sort" || k === "order") paneLocal.set(k, v);
    else merged.push([k, v]);
  }
  const paneQ = paneLocal.toString();
  const seg = encodePaneSegment(fsPath, paneQ ? "?" + paneQ : "");
  navigateUrl(layoutUrl(seg, merged));
}

// Shows the "Update bookmark" button when the armed bookmark's saved params
// diverge from the current url (same pathname). Navigating to a different
// pathname disarms permanently. Called after every renderBreadcrumb() and on
// the "fused:urlchange" event (see main.js).
export function syncUpdateButton() {
  // Embed pages (layout panes included) share the tab's sessionStorage. Their
  // breadcrumb is hidden chrome (D39) — if this ran there, the pane's /embed
  // pathname would never match the armed url and the pathname check below
  // would permanently disarm the bookmark for the whole tab.
  if (IS_EMBED) return;
  const btn = document.getElementById("update-bookmark-btn");
  if (!btn) return;
  const hide = () => {
    btn.style.display = "none";
  };

  const armed = getArmedBookmark();
  if (!armed) return hide();

  // allBookmarks(), not loadBookmarks(): the armed bookmark may live inside a
  // folder (D44), and the top-level list alone would misread it as deleted —
  // disarming on every sync and making the button unreachable.
  const bookmark = allBookmarks().find((b) => b.id === armed.id);
  if (!bookmark) {
    disarmBookmark(); // bookmark deleted out from under us
    return hide();
  }

  // Split armed.url into pathname/search; search keeps its leading "?" or "".
  const qIdx = armed.url.indexOf("?");
  const armedPathname = qIdx === -1 ? armed.url : armed.url.slice(0, qIdx);
  const armedSearch = qIdx === -1 ? "" : armed.url.slice(qIdx);

  if (location.pathname !== armedPathname) {
    disarmBookmark(); // page change = permanent disarm
    return hide();
  }

  // Compare param SETS, not raw strings: the layout view and the runtime
  // encode the same search differently (readable `_layout` vs
  // URLSearchParams.toString()'s full percent-encoding), so a textual compare
  // would flag divergence when nothing changed.
  btn.style.display = sameSearch(location.search, armedSearch) ? "none" : "";
}

// True when two query strings carry the same key/value multiset, ignoring
// encoding and ordering differences (URLSearchParams decodes both sides).
function sameSearch(a, b) {
  const norm = (s) => JSON.stringify([...new URLSearchParams(s)].sort());
  return norm(a) === norm(b);
}
