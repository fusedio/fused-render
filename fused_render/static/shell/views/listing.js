// Directory listing view with sortable columns.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state.
import { navigate } from "../router.js";
import { listDir } from "../api.js";
import { escapeHtml, formatSize, formatMtime } from "../format.js";
import { renderBreadcrumb } from "../breadcrumb.js";

const contentEl = document.getElementById("content");

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

export async function renderListing(fsPath) {
  renderBreadcrumb(fsPath);
  contentEl.innerHTML = `<div class="status-message">Loading…</div>`;
  let data;
  try {
    data = await listDir(fsPath);
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
