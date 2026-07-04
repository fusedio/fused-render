// File preview. Dispatch is exactly three-way (see ARCHITECTURE.md §6):
//   1. stat.template != null -> render template in iframe (+_file on iframe URL)
//   2. .html/.htm             -> render the file itself in iframe
//   3. else                   -> fallback metadata card
// No other file-type logic lives in the shell.
import { rawUrl } from "../api.js";
import { escapeHtml, formatSize, formatMtime } from "../format.js";
import { renderBreadcrumb } from "../breadcrumb.js";

const contentEl = document.getElementById("content");

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
  contentEl.innerHTML = `
    ${header(fsPath, stat)}
    <div class="preview-body">
      <iframe src="${src}"></iframe>
    </div>`;
}

function renderHtmlPreview(fsPath, stat) {
  const toggleHtml = `
    <button id="btn-rendered" class="active">Rendered</button>
    <button id="btn-source">Source</button>`;
  contentEl.innerHTML = `
    ${header(fsPath, stat, toggleHtml)}
    <div class="preview-body">
      <iframe src="/render?path=${encodeURIComponent(fsPath)}"></iframe>
    </div>`;

  const body = contentEl.querySelector(".preview-body");
  const btnRendered = document.getElementById("btn-rendered");
  const btnSource = document.getElementById("btn-source");

  btnRendered.addEventListener("click", () => {
    if (btnRendered.classList.contains("active")) return;
    btnRendered.classList.add("active");
    btnSource.classList.remove("active");
    body.innerHTML = `<iframe src="/render?path=${encodeURIComponent(fsPath)}"></iframe>`;
  });

  btnSource.addEventListener("click", async () => {
    if (btnSource.classList.contains("active")) return;
    btnSource.classList.add("active");
    btnRendered.classList.remove("active");
    body.innerHTML = `<div class="status-message">Loading…</div>`;
    try {
      const res = await fetch(rawUrl(fsPath));
      const text = await res.text();
      body.innerHTML = `<pre class="source">${escapeHtml(text)}</pre>`;
    } catch (err) {
      body.innerHTML = `<div class="status-message error">Failed to load source: ${escapeHtml(err.message)}</div>`;
    }
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
