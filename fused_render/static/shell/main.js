// Entry point: loads config, owns the route() dispatcher.
//   "/"            -> redirect (replaceState) to /view/<start-dir>
//   "/view/<path>" -> stat it: directory -> listing, file -> preview
import { setRouteHandler, fsPathFromLocation, urlForFsPath } from "./router.js";
import { getConfig, statPath } from "./api.js";
import { escapeHtml } from "./format.js";
import { initSidebar, renderSidebar } from "./sidebar.js";
import { renderBreadcrumb } from "./breadcrumb.js";
import { renderListing } from "./views/listing.js";
import { renderPreview } from "./views/preview.js";

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
  renderSidebar(); // refresh active-bookmark highlight for the new URL
}

async function init() {
  config = await getConfig();
  initSidebar(config);
  setRouteHandler(route);
  renderSidebar();
  route();
}

init();
