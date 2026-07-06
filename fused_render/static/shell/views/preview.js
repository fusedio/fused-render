// File preview. Dispatch is exactly three-way (see ARCHITECTURE.md §6):
//   1. stat.template != null -> render template in iframe (+_file on iframe URL)
//   2. .html/.htm             -> render the file itself in iframe
//   3. else                   -> fallback metadata card
// No other file-type logic lives in the shell.
import { rawUrl } from "../api.js";
import { navigateUrl } from "../router.js";
import { escapeHtml, formatSize, formatMtime } from "../format.js";
import { renderBreadcrumb } from "../breadcrumb.js";

const contentEl = document.getElementById("content");

// Abs path of the editable code template, used to render the HTML "Source"
// view (code_template maps .html → CM.html()). Set once from /api/config.
let sourceTemplate = null;

export function initPreview(config) {
  sourceTemplate = config.source_template;
}

function header(fsPath, stat, extraActionsHtml) {
  return `
    <div class="preview-header">
      <h1 title="${escapeHtml(fsPath)}">${escapeHtml(stat.name)}</h1>
      <div class="preview-actions">
        ${extraActionsHtml || ""}
      </div>
    </div>`;
}

function renderTemplatePreview(fsPath, stat) {
  // Target file rides on the iframe's own URL, not the shell URL — the shell
  // URL's pathname already names the file, so no ?_file= duplication there.
  const src = `/render?path=${encodeURIComponent(stat.template)}&_file=${encodeURIComponent(fsPath)}`;
  // A directory preview (e.g. a .zarr store) keeps a way into the raw contents:
  // ?listing=1 forces the shell's listing view for this same path (main.js).
  // The header button serves normal view mode; embed mode hides the whole
  // header (shell.css `body.embed .preview-header`), so a directory template
  // also gets an unobtrusive corner chip over the preview that CSS reveals only
  // in embed — keeping the PT-6/D52 "browse contents" promise reachable there.
  // A file (non-directory) template renders neither, so embed stays chrome-free.
  const actions = stat.is_dir
    ? `<button id="browse-contents" type="button">Browse contents</button>`
    : "";
  const chip = stat.is_dir
    ? `<button id="browse-contents-embed" type="button" class="preview-browse-chip">Browse contents</button>`
    : "";
  contentEl.innerHTML = `
    ${header(fsPath, stat, actions)}
    <div class="preview-body">
      <iframe src="${src}"></iframe>
      ${chip}
    </div>`;
  if (stat.is_dir) {
    const browse = () => navigateUrl(location.pathname + "?listing=1");
    document.getElementById("browse-contents").addEventListener("click", browse);
    document.getElementById("browse-contents-embed").addEventListener("click", browse);
  }
}

function renderHtmlPreview(fsPath, stat) {
  // `_mode` is a reserved shell param (runtime already hides all `_`-prefixed
  // keys from fused.params). It rides the shell URL so the Rendered/Source
  // choice is bookmarkable: ?_mode=source opens straight into the source view.
  const currentMode = () =>
    new URLSearchParams(location.search).get("_mode") === "source" ? "source" : "render";

  const toggleHtml = `
    <button id="btn-rendered" class="${currentMode() === "render" ? "active" : ""}">Rendered</button>
    <button id="btn-source" class="${currentMode() === "source" ? "active" : ""}">Source</button>`;
  contentEl.innerHTML = `
    ${header(fsPath, stat, toggleHtml)}
    <div class="preview-body"></div>`;

  const body = contentEl.querySelector(".preview-body");
  const btnRendered = document.getElementById("btn-rendered");
  const btnSource = document.getElementById("btn-source");

  // Source view is the code template pointed at the HTML file — an editable
  // CodeMirror buffer, same as opening any .py/.js/etc. (_file rides on the
  // iframe URL, like renderTemplatePreview).
  const iframeSrc = (mode) =>
    mode === "source"
      ? `/render?path=${encodeURIComponent(sourceTemplate)}&_file=${encodeURIComponent(fsPath)}`
      : `/render?path=${encodeURIComponent(fsPath)}`;

  // Sync the shell URL (writeUrl only — the initial render must not rewrite
  // it, only clicks do; replaceState per the D8 no-history convention), then
  // the iframe and button classes. Switching to render DELETES _mode (absent =
  // default, keeps URLs clean); switching to source sets it. All other query
  // params are preserved.
  function setMode(mode, writeUrl) {
    if (writeUrl) {
      const params = new URLSearchParams(location.search);
      if (mode === "source") params.set("_mode", "source");
      else params.delete("_mode");
      const search = params.toString();
      history.replaceState(null, "", location.pathname + (search ? "?" + search : ""));
    }
    body.innerHTML = `<iframe src="${iframeSrc(mode)}"></iframe>`;
    btnRendered.classList.toggle("active", mode === "render");
    btnSource.classList.toggle("active", mode === "source");
  }

  // Initial render honors the URL but must not rewrite it.
  setMode(currentMode(), false);

  btnRendered.addEventListener("click", () => {
    if (btnRendered.classList.contains("active")) return;
    setMode("render", true);
  });
  btnSource.addEventListener("click", () => {
    if (btnSource.classList.contains("active")) return;
    setMode("source", true);
  });
}

function renderFallbackPreview(fsPath, stat) {
  contentEl.innerHTML = `
    ${header(fsPath, stat)}
    <div class="preview-body">
      <div class="metadata-card">
        <dl>
          <dt>Name</dt><dd>${escapeHtml(stat.name)}</dd>
          <dt>Path</dt><dd>${escapeHtml(fsPath)}</dd>
          <dt>Size</dt><dd>${formatSize(stat.size)}</dd>
          <dt>Modified</dt><dd>${formatMtime(stat.mtime)}</dd>
        </dl>
        <a href="${rawUrl(fsPath)}" download="${escapeHtml(stat.name)}">Download</a>
      </div>
    </div>`;
}

export function renderPreview(fsPath, stat) {
  renderBreadcrumb(fsPath);
  const ext = fsPath.toLowerCase().split(".").pop();
  if (stat.template) {
    renderTemplatePreview(fsPath, stat);
  } else if (ext === "html" || ext === "htm") {
    renderHtmlPreview(fsPath, stat);
  } else {
    renderFallbackPreview(fsPath, stat);
  }
}
