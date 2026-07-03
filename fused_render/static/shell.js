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
  const BOOKMARKS_KEY = "fused.bookmarks";
  const breadcrumbEl = document.getElementById("breadcrumb");
  const contentEl = document.getElementById("content");
  const sidebarEl = document.getElementById("sidebar");

  let config = null;

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

  function basename(fsPath) {
    const parts = fsPath.split("/").filter((s) => s.length > 0);
    return parts.length ? parts[parts.length - 1] : "/";
  }

  // ---- bookmarks (localStorage) ----------------------------------------------

  function loadBookmarks() {
    try {
      const raw = localStorage.getItem(BOOKMARKS_KEY);
      if (!raw) return [];
      const parsed = JSON.parse(raw);
      return Array.isArray(parsed) ? parsed : [];
    } catch {
      return []; // corrupt JSON -> treat as empty; next save overwrites it
    }
  }

  function saveBookmarks(bookmarks) {
    try {
      localStorage.setItem(BOOKMARKS_KEY, JSON.stringify(bookmarks));
    } catch (e) {
      console.error("[fused] failed to save bookmarks:", e);
    }
  }

  function addBookmark(fsPath) {
    const bookmarks = loadBookmarks();
    bookmarks.push({
      id: crypto.randomUUID(),
      name: basename(fsPath),
      url: location.pathname + location.search,
      created_at: Date.now(),
    });
    saveBookmarks(bookmarks);
    renderSidebar();
  }

  function deleteBookmark(id) {
    saveBookmarks(loadBookmarks().filter((b) => b.id !== id));
    renderSidebar();
  }

  function renameBookmark(id, name) {
    const bookmarks = loadBookmarks();
    const bookmark = bookmarks.find((b) => b.id === id);
    if (bookmark) bookmark.name = name;
    saveBookmarks(bookmarks);
    renderSidebar();
  }

  // ---- sidebar ----------------------------------------------------------------

  function currentUrl() {
    return location.pathname + location.search;
  }

  // Hover card showing a bookmark's target path + saved params.
  const tooltipEl = document.createElement("div");
  tooltipEl.id = "bookmark-tooltip";
  document.body.appendChild(tooltipEl);

  function showBookmarkTooltip(row, bookmark) {
    let pathname = bookmark.url;
    let search = "";
    const qIdx = bookmark.url.indexOf("?");
    if (qIdx !== -1) {
      pathname = bookmark.url.slice(0, qIdx);
      search = bookmark.url.slice(qIdx);
    }
    const fsPath = pathname.startsWith(VIEW_PREFIX)
      ? "/" + pathname.slice(VIEW_PREFIX.length).split("/").map(decodeURIComponent).join("/")
      : pathname;

    const params = [...new URLSearchParams(search)];
    const paramsHtml = params.length
      ? `<div class="tip-params">${params
          .map(([k, v]) => `<span class="tip-key">${escapeHtml(k)}</span><span class="tip-val">${escapeHtml(v)}</span>`)
          .join("")}</div>`
      : `<div class="tip-none">no params</div>`;

    tooltipEl.innerHTML = `<div class="tip-path">${escapeHtml(fsPath)}</div>${paramsHtml}`;
    tooltipEl.style.display = "block";
    const rect = row.getBoundingClientRect();
    tooltipEl.style.left = `${rect.right + 8}px`;
    const top = Math.min(rect.top, window.innerHeight - tooltipEl.offsetHeight - 12);
    tooltipEl.style.top = `${Math.max(8, top)}px`;
  }

  function hideBookmarkTooltip() {
    tooltipEl.style.display = "none";
  }

  function renderSidebar() {
    const bookmarks = loadBookmarks(); // insertion order == creation-time order
    const rows = bookmarks
      .map(
        (b) => `
        <div class="bookmark-row${b.url === currentUrl() ? " active" : ""}" data-id="${escapeHtml(b.id)}">
          <span class="bookmark-glyph">&#9733;</span>
          <a class="bookmark-name" href="${escapeHtml(b.url)}">${escapeHtml(b.name)}</a>
          <span class="bookmark-actions">
            <button class="icon-btn rename-btn" title="Rename">&#9998;</button>
            <button class="icon-btn delete-btn" title="Delete">&#10005;</button>
          </span>
        </div>`
      )
      .join("");

    sidebarEl.innerHTML = `
      <div class="sidebar-brand"><span class="logo">&#10022;</span> fused-render</div>
      <div class="sidebar-section">
        <a href="#" id="home-link" class="sidebar-item"><span class="icon">&#127968;</span> Home</a>
      </div>
      <div class="sidebar-section sidebar-bookmarks">
        <div class="sidebar-heading">Bookmarks</div>
        ${rows || `<div class="sidebar-empty">No bookmarks yet</div>`}
      </div>`;

    document.getElementById("home-link").addEventListener("click", (e) => {
      e.preventDefault();
      if (config && config.home) navigate(config.home);
    });

    const byId = new Map(bookmarks.map((b) => [b.id, b]));
    sidebarEl.querySelectorAll(".bookmark-row").forEach((row) => {
      const id = row.getAttribute("data-id");
      row.querySelector(".delete-btn").addEventListener("click", (e) => {
        e.preventDefault();
        hideBookmarkTooltip();
        deleteBookmark(id);
      });
      row.querySelector(".rename-btn").addEventListener("click", (e) => {
        e.preventDefault();
        hideBookmarkTooltip();
        startRename(row, id);
      });
      row.addEventListener("mouseenter", () => {
        // No tooltip while renaming this row.
        if (!row.querySelector(".bookmark-rename-input")) showBookmarkTooltip(row, byId.get(id));
      });
      row.addEventListener("mouseleave", hideBookmarkTooltip);
    });

    syncStarButton(bookmarks);
  }

  function syncStarButton(bookmarks) {
    const btn = document.getElementById("bookmark-btn");
    if (!btn) return;
    const starred = (bookmarks || loadBookmarks()).some((b) => b.url === currentUrl());
    btn.classList.toggle("starred", starred);
    btn.title = starred ? "View is bookmarked (★ adds another)" : "Bookmark this view";
  }

  function startRename(row, id) {
    const bookmark = loadBookmarks().find((b) => b.id === id);
    if (!bookmark) return;
    const nameEl = row.querySelector(".bookmark-name");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "bookmark-rename-input";
    input.value = bookmark.name;
    nameEl.replaceWith(input);
    input.focus();
    input.select();

    let settled = false;
    const commit = () => {
      if (settled) return;
      settled = true;
      renameBookmark(id, input.value.trim() || bookmark.name);
    };
    const cancel = () => {
      if (settled) return;
      settled = true;
      renderSidebar();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        commit();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    });
    input.addEventListener("blur", commit);
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
    breadcrumbEl.innerHTML = `
      <div class="crumbs">${pieces.join("")}</div>
      <button id="bookmark-btn" class="star-btn" title="Bookmark this view">+ Bookmark</button>`;
    breadcrumbEl.querySelectorAll("a[data-path]").forEach((a) => {
      a.addEventListener("click", (e) => {
        e.preventDefault();
        navigate(a.getAttribute("data-path"));
      });
    });
    document.getElementById("bookmark-btn").addEventListener("click", () => {
      addBookmark(fsPath);
    });
    syncStarButton();
  }

  // ---- listing view -----------------------------------------------------------

  // Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
  // sorted listing is refresh-proof and bookmarkable like any other view state.
  const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };

  function currentSort() {
    const q = new URLSearchParams(location.search);
    const sort = SORT_KEYS[q.get("sort")] ? q.get("sort") : "name";
    const order = q.get("order") === "desc" ? "desc" : "asc";
    return { sort, order };
  }

  function setSort(key) {
    const { sort, order } = currentSort();
    const q = new URLSearchParams(location.search);
    q.set("sort", key);
    q.set("order", key === sort && order === "asc" ? "desc" : "asc");
    history.replaceState(null, "", location.pathname + "?" + q.toString());
  }

  function sortEntries(entries, sort, order) {
    const flip = order === "desc" ? -1 : 1;
    const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
    return [...entries].sort((a, b) => {
      if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // dirs always group first
      let cmp;
      if (sort === "size") cmp = (a.size ?? -1) - (b.size ?? -1);
      else if (sort === "mtime") cmp = (a.mtime ?? 0) - (b.mtime ?? 0);
      else cmp = byName(a, b);
      if (cmp === 0) cmp = byName(a, b);
      return cmp * flip;
    });
  }

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

    const { sort, order } = currentSort();
    const rows = sortEntries(data.entries, sort, order)
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

    const headers = Object.entries(SORT_KEYS)
      .map(([key, label]) => {
        const arrow = key === sort ? `<span class="sort-arrow">${order === "asc" ? "▲" : "▼"}</span>` : "";
        return `<th class="sortable${key === sort ? " sorted" : ""}" data-sort="${key}">${label}${arrow}</th>`;
      })
      .join("");

    contentEl.innerHTML = `
      <div class="listing">
        <table class="listing-table">
          <thead><tr>${headers}</tr></thead>
          <tbody>${rows || `<tr><td colspan="3" class="status-message">Empty directory</td></tr>`}</tbody>
        </table>
      </div>`;

    contentEl.querySelectorAll("th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        setSort(th.getAttribute("data-sort"));
        renderListing(fsPath);
      });
    });

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
          <a href="${rawUrl(fsPath)}" download="${escapeHtml(stat.name)}">Download</a>
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
      history.replaceState(null, "", urlForFsPath(config.start_dir));
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
    renderSidebar(); // refresh active-bookmark highlight for the new URL
  }

  async function init() {
    config = await fetch("/api/config").then((r) => r.json());
    renderSidebar();
    route();
  }

  init();
})();
