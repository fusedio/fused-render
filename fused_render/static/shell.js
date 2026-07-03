/*
 * Explorer shell SPA. Routes purely off `location.pathname`:
 *   "/"            -> redirect (replaceState) to /view/<start-dir>
 *   "/view/<path>" -> stat it: directory -> listing, file -> preview
 *
 * Shell dispatch for previews is exactly three-way (see ARCHITECTURE.md §6):
 *   1. stat.template != null -> render template in iframe, merge ?_file=
 *   2. .html/.htm             -> render the file itself in iframe
 *   3. else                   -> fallback metadata card
 * No other file-type logic lives here.
 */
(function () {
  "use strict";

  const VIEW_PREFIX = "/view/";
  const breadcrumbEl = document.getElementById("breadcrumb");
  const contentEl = document.getElementById("content");

  // ---- fs-path <-> URL pathname -------------------------------------------

  function fsPathFromLocation() {
    const p = location.pathname;
    if (!p.startsWith(VIEW_PREFIX)) return null;
    const rest = p.slice(VIEW_PREFIX.length);
    const decoded = rest
      .split("/")
      .filter((s) => s.length > 0)
      .map(decodeURIComponent)
      .join("/");
    return "/" + decoded;
  }

  function urlForFsPath(fsPath, search) {
    const rest = fsPath.replace(/^\/+/, "");
    const encoded = rest
      .split("/")
      .filter((s) => s.length > 0)
      .map(encodeURIComponent)
      .join("/");
    return VIEW_PREFIX + encoded + (search || "");
  }

  function navigate(fsPath) {
    // Navigating between files/dirs drops old view params (fresh query string).
    history.pushState(null, "", urlForFsPath(fsPath));
    route();
  }

  window.addEventListener("popstate", route);

  // ---- formatting helpers --------------------------------------------------

  function formatSize(bytes) {
    if (bytes === null || bytes === undefined) return "";
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let v = bytes;
    let u = -1;
    do {
      v /= 1024;
      u++;
    } while (v >= 1024 && u < units.length - 1);
    return `${v.toFixed(v < 10 ? 1 : 0)} ${units[u]}`;
  }

  function formatMtime(epochSeconds) {
    if (!epochSeconds) return "";
    const d = new Date(epochSeconds * 1000);
    return d.toLocaleString();
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function rawUrl(fsPath) {
    return "/api/fs/raw?path=" + encodeURIComponent(fsPath);
  }

  // ---- breadcrumb -----------------------------------------------------------

  function renderBreadcrumb(fsPath) {
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
    breadcrumbEl.innerHTML = pieces.join("");
    breadcrumbEl.querySelectorAll("a[data-path]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        navigate(a.getAttribute("data-path"));
      });
    });
  }

  // ---- listing view -----------------------------------------------------------

  async function renderListing(fsPath) {
    renderBreadcrumb(fsPath);
    contentEl.innerHTML = `<div class="status-message">Loading…</div>`;
    let data;
    try {
      const res = await fetch("/api/fs/list?path=" + encodeURIComponent(fsPath));
      data = await res.json();
      if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    } catch (err) {
      contentEl.innerHTML = `<div class="status-message error">Failed to list ${escapeHtml(fsPath)}: ${escapeHtml(err.message)}</div>`;
      return;
    }

    const rows = data.entries
      .map((entry) => {
        const childPath = fsPath.replace(/\/$/, "") + "/" + entry.name;
        const icon = entry.is_dir ? "\u{1F4C1}" : "\u{1F4C4}";
        return `<tr class="row" data-path="${escapeHtml(childPath)}">
          <td class="name"><span class="icon">${icon}</span>${escapeHtml(entry.name)}</td>
          <td class="size">${entry.is_dir ? "" : formatSize(entry.size)}</td>
          <td class="mtime">${formatMtime(entry.mtime)}</td>
        </tr>`;
      })
      .join("");

    contentEl.innerHTML = `
      <div class="listing">
        <table class="listing-table">
          <thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead>
          <tbody>${rows || `<tr><td colspan="3" class="status-message">Empty directory</td></tr>`}</tbody>
        </table>
      </div>`;

    contentEl.querySelectorAll("tr.row[data-path]").forEach((tr) => {
      tr.addEventListener("click", () => navigate(tr.getAttribute("data-path")));
    });
  }

  // ---- preview view -----------------------------------------------------------

  function mergeFileParam(fsPath) {
    const params = new URLSearchParams(location.search);
    params.set("_file", fsPath);
    history.replaceState(null, "", location.pathname + "?" + params.toString());
  }

  function renderPreviewHeader(fsPath, stat, extraActionsHtml) {
    return `
      <div class="preview-header">
        <h1 title="${escapeHtml(fsPath)}">${escapeHtml(stat.name)}</h1>
        <div class="preview-actions">
          ${extraActionsHtml || ""}
          <a href="${rawUrl(fsPath)}" target="_blank" rel="noopener">Raw</a>
        </div>
      </div>`;
  }

  function renderTemplatePreview(fsPath, stat) {
    mergeFileParam(fsPath);
    contentEl.innerHTML = `
      ${renderPreviewHeader(fsPath, stat)}
      <div class="preview-body">
        <iframe src="/render?path=${encodeURIComponent(stat.template)}"></iframe>
      </div>`;
  }

  function renderHtmlPreview(fsPath, stat) {
    const toggleHtml = `
      <button id="btn-rendered" class="active">Rendered</button>
      <button id="btn-source">Source</button>`;
    contentEl.innerHTML = `
      ${renderPreviewHeader(fsPath, stat, toggleHtml)}
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
      ${renderPreviewHeader(fsPath, stat)}
      <div class="preview-body">
        <div class="metadata-card">
          <dl>
            <dt>Name</dt><dd>${escapeHtml(stat.name)}</dd>
            <dt>Path</dt><dd>${escapeHtml(fsPath)}</dd>
            <dt>Size</dt><dd>${formatSize(stat.size)}</dd>
            <dt>Modified</dt><dd>${formatMtime(stat.mtime)}</dd>
          </dl>
          <a href="${rawUrl(fsPath)}" target="_blank" rel="noopener">Raw / download</a>
        </div>
      </div>`;
  }

  function renderPreview(fsPath, stat) {
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

  // ---- router -----------------------------------------------------------------

  async function route() {
    if (location.pathname === "/") {
      const cfg = await fetch("/api/config").then((r) => r.json());
      history.replaceState(null, "", urlForFsPath(cfg.start_dir));
    }

    const fsPath = fsPathFromLocation();
    if (!fsPath) {
      contentEl.innerHTML = `<div class="status-message error">Unrecognized URL: ${escapeHtml(location.pathname)}</div>`;
      return;
    }

    let stat;
    try {
      const res = await fetch("/api/fs/stat?path=" + encodeURIComponent(fsPath));
      stat = await res.json();
      if (!res.ok) throw new Error(stat.error || `HTTP ${res.status}`);
    } catch (err) {
      renderBreadcrumb(fsPath);
      contentEl.innerHTML = `<div class="status-message error">Failed to stat ${escapeHtml(fsPath)}: ${escapeHtml(err.message)}</div>`;
      return;
    }

    if (stat.is_dir) {
      renderListing(fsPath);
    } else {
      renderPreview(fsPath, stat);
    }
  }

  route();
})();
