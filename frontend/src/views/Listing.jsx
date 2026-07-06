// Directory listing view with sortable columns.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state.
import React, { useEffect, useState } from "react";
import { navigate } from "../lib/router.js";
import { listDir } from "../lib/api.js";
import { formatSize, formatMtime } from "../lib/format.js";

const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };

function currentSort() {
  const q = new URLSearchParams(location.search);
  const sort = SORT_KEYS[q.get("sort")] ? q.get("sort") : "name";
  const order = q.get("order") === "desc" ? "desc" : "asc";
  return { sort, order };
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

export default function Listing({ fsPath }) {
  const [state, setState] = useState({ status: "loading" });
  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState(currentSort);
  const [refresh, setRefresh] = useState(0); // bumped by the SSE dir watch

  useEffect(() => {
    let alive = true;
    listDir(fsPath).then(
      (data) => alive && setState({ status: "ok", entries: data.entries }),
      (err) => alive && setState({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [fsPath, refresh]);

  // SSE watch on the listed directory (LS-1). A directory's mtime changes on
  // create/delete/rename of entries (not on child content changes — LS-2,
  // accepted). Closed on unmount = navigating away (LS-3). On change,
  // debounce 300 ms then re-fetch; sort params live in URL + state, so a
  // refetch preserves them.
  useEffect(() => {
    const es = new EventSource("/api/fs/events?path=" + encodeURIComponent(fsPath));
    let timer = null;
    es.onmessage = () => {
      clearTimeout(timer);
      timer = setTimeout(() => setRefresh((n) => n + 1), 300);
    };
    return () => {
      clearTimeout(timer);
      es.close();
    };
  }, [fsPath]);

  const setSort = (key) => {
    const next = {
      sort: key,
      order: key === sort && order === "asc" ? "desc" : "asc",
    };
    const q = new URLSearchParams(location.search);
    q.set("sort", next.sort);
    q.set("order", next.order);
    history.replaceState(null, "", location.pathname + "?" + q.toString());
    setSortState(next);
  };

  if (state.status === "loading") return <div className="status-message">Loading…</div>;
  if (state.status === "error")
    return (
      <div className="status-message error">
        Failed to list {fsPath}: {state.message}
      </div>
    );

  const rows = sortEntries(state.entries, sort, order).map((entry) => {
    const childPath = fsPath.replace(/\/$/, "") + "/" + entry.name;
    return (
      <tr key={entry.name} className="row" onClick={() => navigate(childPath)}>
        <td className="name">
          <span className="icon">{entry.is_dir ? "\u{1F4C1}" : "\u{1F4C4}"}</span>
          {entry.name}
        </td>
        <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
        <td className="mtime">{formatMtime(entry.mtime)}</td>
      </tr>
    );
  });

  return (
    <div className="listing">
      <table className="listing-table">
        <thead>
          <tr>
            {Object.entries(SORT_KEYS).map(([key, label]) => (
              <th
                key={key}
                className={"sortable" + (key === sort ? " sorted" : "")}
                onClick={() => setSort(key)}
              >
                {label}
                {key === sort && <span className="sort-arrow">{order === "asc" ? "▲" : "▼"}</span>}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows
          ) : (
            <tr>
              <td colSpan={3} className="status-message">
                Empty directory
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}
