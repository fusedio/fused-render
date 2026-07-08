// Directory listing view with sortable columns.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state.
import { useEffect, useState } from "react";
import { navigate } from "../lib/router";
import { listDir } from "../lib/api";
import type { FsEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";

const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };
type SortKey = keyof typeof SORT_KEYS;
type SortOrder = "asc" | "desc";

function currentSort(): { sort: SortKey; order: SortOrder } {
  const q = new URLSearchParams(location.search);
  const key = q.get("sort");
  const sort: SortKey = key && key in SORT_KEYS ? (key as SortKey) : "name";
  const order: SortOrder = q.get("order") === "desc" ? "desc" : "asc";
  return { sort, order };
}

function sortEntries(entries: FsEntry[], sort: SortKey, order: SortOrder): FsEntry[] {
  const flip = order === "desc" ? -1 : 1;
  const byName = (a: FsEntry, b: FsEntry) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  return [...entries].sort((a, b) => {
    const aDot = a.name.startsWith(".");
    const bDot = b.name.startsWith(".");
    if (aDot !== bDot) return aDot ? 1 : -1; // dot entries always group last
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // dirs group first within each
    let cmp: number;
    if (sort === "size") cmp = (a.size ?? -1) - (b.size ?? -1);
    else if (sort === "mtime") cmp = (a.mtime ?? 0) - (b.mtime ?? 0);
    else cmp = byName(a, b);
    if (cmp === 0) cmp = byName(a, b);
    return cmp * flip;
  });
}

type ListingState =
  | { status: "loading" }
  | { status: "ok"; entries: FsEntry[] }
  | { status: "error"; message: string };

export default function Listing({ fsPath }: { fsPath: string }) {
  const [state, setState] = useState<ListingState>({ status: "loading" });
  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{ sort: SortKey; order: SortOrder }>(currentSort);
  const [refresh, setRefresh] = useState(0); // bumped by the SSE dir watch

  useEffect(() => {
    let alive = true;
    listDir(fsPath).then(
      (data) => alive && setState({ status: "ok", entries: data.entries }),
      (err: Error) => alive && setState({ status: "error", message: err.message })
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
    let timer: ReturnType<typeof setTimeout> | null = null;
    es.onmessage = () => {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => setRefresh((n) => n + 1), 300);
    };
    return () => {
      if (timer !== null) clearTimeout(timer);
      es.close();
    };
  }, [fsPath]);

  const setSort = (key: SortKey) => {
    const next: { sort: SortKey; order: SortOrder } = {
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
            {(Object.entries(SORT_KEYS) as [SortKey, string][]).map(([key, label]) => (
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
