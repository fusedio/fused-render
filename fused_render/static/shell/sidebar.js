// Sidebar UI: brand, Home entry, bookmark rows with hover card + inline rename.
import { navigate, currentUrl, VIEW_PREFIX } from "./router.js";
import { escapeHtml } from "./format.js";
import { loadBookmarks, deleteBookmark, renameBookmark } from "./bookmarks.js";

const sidebarEl = document.getElementById("sidebar");

let config = null;

export function initSidebar(cfg) {
  config = cfg;
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

export function renderSidebar() {
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
      renderSidebar();
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

export function syncStarButton(bookmarks) {
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
    renderSidebar();
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
