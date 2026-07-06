// Entry point: loads config, owns the route() dispatcher.
//   "/"            -> redirect (replaceState) to /view/<start-dir>
//   "/view/<path>" -> stat it: directory -> listing, file -> preview
import { setRouteHandler, fsPathFromLocation, urlForFsPath, IS_EMBED } from "./router.js";
import { getConfig, statPath } from "./api.js";
import { escapeHtml } from "./format.js";
import { initSidebar, renderSidebar } from "./sidebar.js";
import { renderBreadcrumb, syncUpdateButton } from "./breadcrumb.js";
import { renderListing } from "./views/listing.js";
import { renderPreview, initPreview } from "./views/preview.js";

const contentEl = document.getElementById("content");

let config = null;

async function route() {
  if (location.pathname === "/") {
    history.replaceState(null, "", urlForFsPath(config.start_dir));
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
  // changes so the "Update bookmark" button can react to them.
  const origReplaceState = history.replaceState.bind(history);
  history.replaceState = function (...args) {
    origReplaceState(...args);
    window.dispatchEvent(new Event("fused:urlchange"));
  };
  window.addEventListener("fused:urlchange", () => syncUpdateButton());

  if (!IS_EMBED) renderSidebar();
  route();
}

init();
