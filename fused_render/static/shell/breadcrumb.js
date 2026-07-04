// Crumb bar + "+ Bookmark" button. Rendered by every view.
import { navigate, currentUrl } from "./router.js";
import { escapeHtml, basename } from "./format.js";
import { addBookmark } from "./bookmarks.js";
import { renderSidebar, syncStarButton } from "./sidebar.js";

const breadcrumbEl = document.getElementById("breadcrumb");

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
    <button id="bookmark-btn" class="star-btn" title="Bookmark this view">+ Bookmark</button>`;
  breadcrumbEl.querySelectorAll("a[data-path]").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navigate(a.getAttribute("data-path"));
    });
  });
  document.getElementById("bookmark-btn").addEventListener("click", () => {
    addBookmark(basename(fsPath), currentUrl());
    renderSidebar();
  });
  syncStarButton();
}
