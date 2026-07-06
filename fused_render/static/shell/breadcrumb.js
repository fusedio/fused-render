// Crumb bar + "+ Bookmark" button. Rendered by every view.
import { navigate, currentUrl } from "./router.js";
import { escapeHtml, basename } from "./format.js";
import { addBookmark, loadBookmarks, updateBookmarkUrl, armBookmark, disarmBookmark, getArmedBookmark } from "./bookmarks.js";
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
    <div class="crumb-actions">
      <button id="update-bookmark-btn" class="star-btn starred" title="Update bookmark to current params" style="display:none">Update bookmark</button>
      <button id="bookmark-btn" class="star-btn" title="Bookmark this view">+ Bookmark</button>
    </div>`;
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

// Shows the "Update bookmark" button when the armed bookmark's saved params
// diverge from the current url (same pathname). Navigating to a different
// pathname disarms permanently. Called after every renderBreadcrumb() and on
// the "fused:urlchange" event (see main.js).
export function syncUpdateButton() {
  const btn = document.getElementById("update-bookmark-btn");
  if (!btn) return;
  const hide = () => {
    btn.style.display = "none";
  };

  const armed = getArmedBookmark();
  if (!armed) return hide();

  const bookmark = loadBookmarks().find((b) => b.id === armed.id);
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

  // location.search is "" or "?..." — same normalization as armedSearch.
  btn.style.display = location.search !== armedSearch ? "" : "none";
}
