// Entry point: loads config, owns the route() dispatcher.
//   "/"            -> redirect (replaceState) to /view/<start-dir>
//   "/view/<path>" -> stat it: directory -> listing, file -> preview
import { setRouteHandler, fsPathFromLocation, urlForFsPath, IS_EMBED } from "./router.js";
import { getConfig, statPath } from "./api.js";
import { escapeHtml } from "./format.js";
import { initSidebar, renderSidebar } from "./sidebar.js";
import { renderBreadcrumb, renderLayoutBreadcrumb, renderTabsBreadcrumb, syncUpdateButton } from "./breadcrumb.js";
import { renderListing, stopListingWatch } from "./views/listing.js";
import { renderPreview, initPreview } from "./views/preview.js";
import { renderLayout, stopLayout } from "./views/panel.js";
import { renderTabs, stopTabs } from "./views/tabs.js";

const contentEl = document.getElementById("content");

let config = null;

async function route() {
  stopListingWatch(); // close any listing watch when navigating away (LS-3)
  stopLayout(); // detach layout pane listeners when navigating away (LM-6)
  stopTabs(); // detach tab listeners when navigating away (TM-7)
  if (location.pathname === "/") {
    history.replaceState(null, "", urlForFsPath(config.start_dir));
  }

  // Layout mode: `_panel` is a sentinel pathname, not a real file (LM-1) —
  // intercept it before stat, under both prefixes so a layout can itself be
  // embedded (or nested as a pane). Zero server changes: the server already
  // serves the shell for any /view/* and /embed/*.
  if (location.pathname === "/view/_panel" || location.pathname === "/embed/_panel") {
    renderLayoutBreadcrumb();
    renderLayout(config);
    if (!IS_EMBED) renderSidebar();
    return;
  }

  // Tab mode: `_tab` is a sentinel pathname like `_panel` (TM-1), intercepted
  // under both prefixes before stat.
  if (location.pathname === "/view/_tab" || location.pathname === "/embed/_tab") {
    renderTabsBreadcrumb();
    renderTabs(config);
    if (!IS_EMBED) renderSidebar();
    return;
  }

  const fsPath = fsPathFromLocation();
  if (!fsPath) {
    contentEl.innerHTML = `<div class="status-message error">Unrecognized URL: ${escapeHtml(location.pathname)}</div>`;
    return;
  }

  let stat;
  try {
    stat = await statPath(fsPath);
  } catch (err) {
    renderBreadcrumb(fsPath);
    contentEl.innerHTML = `<div class="status-message error">Failed to stat ${escapeHtml(fsPath)}: ${escapeHtml(err.message)}</div>`;
    return;
  }

  if (stat.is_dir) {
    await renderListing(fsPath);
  } else {
    renderPreview(fsPath, stat);
  }
  if (!IS_EMBED) renderSidebar(); // refresh active-bookmark highlight for the new URL
}

async function init() {
  if (IS_EMBED) document.body.classList.add("embed");
  config = await getConfig();
  if (!IS_EMBED) initSidebar(config);
  initPreview(config);
  setRouteHandler(route);

  // The preview iframe's injected runtime writes view params via
  // parent.history.replaceState (same history object), which fires no event.
  // Wrapping replaceState is the shell's only way to observe those param
  // changes so the "Update bookmark" button can react to them. pushState is
  // wrapped the same way so in-pane navigation is observable too — the layout
  // view's runtime target dispatches fused:urlchange through both (LM-8, D46).
  const origReplaceState = history.replaceState.bind(history);
  history.replaceState = function (...args) {
    origReplaceState(...args);
    window.dispatchEvent(new Event("fused:urlchange"));
  };
  const origPushState = history.pushState.bind(history);
  history.pushState = function (...args) {
    origPushState(...args);
    window.dispatchEvent(new Event("fused:urlchange"));
  };
  window.addEventListener("fused:urlchange", () => syncUpdateButton());

  if (!IS_EMBED) renderSidebar();
  route();
}

init();
