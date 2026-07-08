// Directory listing view with sortable columns and an in-folder search.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state;
// the search query rides the URL the same way (?q=…). A non-empty query swaps
// the listing for flat, rank-ordered results over a recursive walk of the
// folder (fetched lazily on first focus, cached until the dir watch fires).
import { useDeferredValue, useEffect, useMemo, useState } from "react";
import { navigate } from "../lib/router";
import { listDir, walkDir } from "../lib/api";
import type { FsEntry, WalkEntry, WalkResult } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import { fuzzyMatch, substringMatch, highlightSegments, isFuzzyEnabled, setFuzzyEnabled } from "../lib/fuzzy";
import type { FuzzyResult } from "../lib/fuzzy";
import { useSearchFuzzyVersion } from "../lib/hooks";

const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };
type SortKey = keyof typeof SORT_KEYS;
type SortOrder = "asc" | "desc";

// Cap on rendered search-result rows. Fuzzy-scoring can match thousands of
// entries in a large tree; rendering all of them as <tr>s is what actually
// jams the main thread (scoring itself is comparatively cheap). The full
// ranked list still exists in memory for the count text.
const MAX_RESULTS = 250;

function currentSort(): { sort: SortKey; order: SortOrder } {
  const q = new URLSearchParams(location.search);
  const key = q.get("sort");
  const sort: SortKey = key && key in SORT_KEYS ? (key as SortKey) : "name";
  const order: SortOrder = q.get("order") === "desc" ? "desc" : "asc";
  return { sort, order };
}

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
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

// Match a query against every walked entry's relative path, then rank: higher
// fuzzy score first, then fewer path segments (shallower = closer to hand),
// then alphabetical for a stable order.
interface SearchHit {
  entry: WalkEntry;
  positions: number[];
}
function searchWalk(
  query: string,
  entries: WalkEntry[],
  matcher: (query: string, text: string) => FuzzyResult | null
): SearchHit[] {
  const hits: { entry: WalkEntry; positions: number[]; score: number }[] = [];
  for (const entry of entries) {
    const m = matcher(query, entry.rel);
    if (m) hits.push({ entry, positions: m.positions, score: m.score });
  }
  hits.sort((a, b) => {
    if (b.score !== a.score) return b.score - a.score;
    const ad = a.entry.rel.split("/").length;
    const bd = b.entry.rel.split("/").length;
    if (ad !== bd) return ad - bd;
    return a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
  });
  return hits.map(({ entry, positions }) => ({ entry, positions }));
}

function renderHighlight(text: string, positions: number[]) {
  return highlightSegments(text, positions).map((seg, i) =>
    seg.match ? (
      <mark key={i} className="search-mark">
        {seg.text}
      </mark>
    ) : (
      <span key={i}>{seg.text}</span>
    )
  );
}

type ListingState =
  | { status: "loading" }
  | { status: "ok"; entries: FsEntry[] }
  | { status: "error"; message: string };

// Cached recursive walk. Reset to "idle" whenever the folder changes or the dir
// watch fires; a focus/search then re-fetches it fresh.
type WalkState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "ok"; result: WalkResult }
  | { status: "error"; message: string };

export default function Listing({ fsPath }: { fsPath: string }) {
  const [state, setState] = useState<ListingState>({ status: "loading" });
  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{ sort: SortKey; order: SortOrder }>(currentSort);
  const [refresh, setRefresh] = useState(0); // bumped by the dir watch socket
  const [query, setQueryState] = useState<string>(currentQuery);
  const [walk, setWalk] = useState<WalkState>({ status: "idle" });
  // Sort applied to search results. null = relevance (fuzzy rank). Deliberately
  // NOT URL-synced (unlike the normal-mode sort) — it resets on every query
  // change, so persisting it would fight that reset.
  const [searchSort, setSearchSort] = useState<{ sort: SortKey; order: SortOrder } | null>(null);
  // Fuzzy on/off is a shared pref (lib/fuzzy.ts), not local state; this hook
  // just forces a re-render when it changes so the toggle takes effect
  // immediately, including when flipped from the sidebar's own toggle.
  useSearchFuzzyVersion();
  const fuzzyOn = isFuzzyEnabled();

  // The input echoes `query` (immediate) so keystrokes never wait on the
  // fuzzy-scoring/rendering work below. `deferredQuery` trails behind under
  // load — React commits a cheap render with the old deferred value first
  // (echoing the keystroke), then a low-priority render picks up the new
  // value and redoes the expensive work, interruptible by further typing.
  const deferredQuery = useDeferredValue(query);
  const q = deferredQuery.trim();
  const searching = q !== "";
  const isStale = query.trim() !== q;

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

  // WebSocket watch on the listed directory (LS-1); WS not SSE per D74 (SSE
  // pinned one of Chrome's 6 HTTP/1.1 sockets per view). A directory's mtime
  // changes on create/delete/rename of entries (not on child content changes
  // — LS-2, accepted). Closed on unmount = navigating away (LS-3). On change,
  // debounce 300 ms then re-fetch; sort params live in URL + state, so a
  // refetch preserves them.
  useEffect(() => {
    let sock: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss://" : "ws://";
      sock = new WebSocket(proto + location.host + "/api/fs/events?path=" + encodeURIComponent(fsPath));
      sock.onmessage = (ev) => {
        let data;
        try {
          data = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (data.keepalive) return;
        if (timer !== null) clearTimeout(timer);
        timer = setTimeout(() => setRefresh((n) => n + 1), 300);
      };
      // WebSockets don't auto-reconnect the way EventSource did.
      sock.onclose = () => {
        if (!closed) retry = setTimeout(connect, 1000);
      };
    };
    connect();
    return () => {
      closed = true;
      if (retry !== null) clearTimeout(retry);
      if (timer !== null) clearTimeout(timer);
      sock?.close();
    };
  }, [fsPath]);

  // Drop the cached walk when the folder changes or the dir watch fires so the
  // next search reflects the current tree (mirrors the listing's invalidation).
  useEffect(() => {
    setWalk({ status: "idle" });
  }, [fsPath, refresh]);

  // Fetch when a walk is requested (focus or a URL-seeded query). Reading
  // "loading" as the trigger keeps the fetch out of a state updater.
  useEffect(() => {
    if (walk.status !== "loading") return;
    let alive = true;
    walkDir(fsPath).then(
      (result) => alive && setWalk({ status: "ok", result }),
      (err: Error) => alive && setWalk({ status: "error", message: err.message })
    );
    return () => {
      alive = false;
    };
  }, [walk.status, fsPath]);

  // A query restored from the URL needs the walk even without a focus event.
  useEffect(() => {
    if (searching) setWalk((prev) => (prev.status === "idle" ? { status: "loading" } : prev));
  }, [searching, walk.status]);

  const prefetchWalk = () => setWalk((prev) => (prev.status === "idle" ? { status: "loading" } : prev));

  const setQuery = (value: string) => {
    setQueryState(value);
    setSearchSort(null); // a new query drops back to relevance order
    const params = new URLSearchParams(location.search);
    if (value) params.set("q", value);
    else params.delete("q");
    const qs = params.toString();
    history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
  };

  const setSort = (key: SortKey) => {
    const next: { sort: SortKey; order: SortOrder } = {
      sort: key,
      order: key === sort && order === "asc" ? "desc" : "asc",
    };
    const params = new URLSearchParams(location.search);
    params.set("sort", next.sort);
    params.set("order", next.order);
    history.replaceState(null, "", location.pathname + "?" + params.toString());
    setSortState(next);
  };

  const setSearchSortKey = (key: SortKey) => {
    setSearchSort((prev) =>
      prev && prev.sort === key
        ? { sort: key, order: prev.order === "asc" ? "desc" : "asc" }
        : { sort: key, order: "asc" }
    );
  };

  // Keyed on `q`/`searching` (both deferred) so the 20k-entry fuzzy scan runs
  // on React's low-priority schedule, not synchronously on every keystroke.
  const hits = useMemo(() => {
    if (!(searching && walk.status === "ok")) return [];
    const matcher = fuzzyOn ? fuzzyMatch : substringMatch;
    const ranked = searchWalk(q, walk.result.entries, matcher);
    if (!searchSort) return ranked; // relevance order
    const { sort, order } = searchSort;
    const flip = order === "desc" ? -1 : 1;
    const byName = (a: SearchHit, b: SearchHit) =>
      a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
    return [...ranked].sort((a, b) => {
      let cmp: number;
      if (sort === "size") cmp = (a.entry.size ?? -1) - (b.entry.size ?? -1);
      else if (sort === "mtime") cmp = (a.entry.mtime ?? 0) - (b.entry.mtime ?? 0);
      else cmp = byName(a, b);
      if (cmp === 0) cmp = byName(a, b);
      return cmp * flip;
    });
  }, [searching, q, walk, searchSort, fuzzyOn]);

  // Rendering every match as a <tr> is the other half of the per-keystroke
  // jank on huge trees (thousands of rows synchronously mounted). Cap what
  // actually hits the DOM; the count text below still reflects the full total.
  const visibleHits = useMemo(() => hits.slice(0, MAX_RESULTS), [hits]);

  // Same idea for the plain (non-search) listing: re-sorting on every render
  // (e.g. a keystroke that flips `searching` before this branch even
  // displays) was pure waste when `state`/sort/order hadn't changed.
  const sortedEntries = useMemo(
    () => (state.status === "ok" ? sortEntries(state.entries, sort, order) : []),
    [state, sort, order]
  );

  const base = fsPath.replace(/\/$/, "");
  const fileIcon = "\u{1F4C4}";
  const dirIcon = "\u{1F4C1}";

  // --- table body -----------------------------------------------------------

  let body: React.ReactNode;
  if (searching) {
    if (walk.status === "error") {
      body = (
        <tr>
          <td colSpan={3} className="status-message error">
            Search failed: {walk.message}
          </td>
        </tr>
      );
    } else if (walk.status === "ok") {
      body = hits.length ? (
        visibleHits.map(({ entry, positions }) => {
          const childPath = base + "/" + entry.rel;
          return (
            <tr key={entry.rel} className="row" onClick={() => navigate(childPath)}>
              <td className="name">
                <span className="icon">{entry.is_dir ? dirIcon : fileIcon}</span>
                <span className="search-path">{renderHighlight(entry.rel, positions)}</span>
              </td>
              <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
              <td className="mtime">{formatMtime(entry.mtime)}</td>
            </tr>
          );
        })
      ) : (
        <tr>
          <td colSpan={3} className="status-message">
            No matches
          </td>
        </tr>
      );
    } else {
      body = (
        <tr>
          <td colSpan={3} className="status-message">
            Searching…
          </td>
        </tr>
      );
    }
  } else if (state.status === "loading") {
    body = (
      <tr>
        <td colSpan={3} className="status-message">
          Loading…
        </td>
      </tr>
    );
  } else if (state.status === "error") {
    body = (
      <tr>
        <td colSpan={3} className="status-message error">
          Failed to list {fsPath}: {state.message}
        </td>
      </tr>
    );
  } else {
    const rows = sortedEntries.map((entry) => {
      const childPath = base + "/" + entry.name;
      return (
        <tr key={entry.name} className="row" onClick={() => navigate(childPath)}>
          <td className="name">
            <span className="icon">{entry.is_dir ? dirIcon : fileIcon}</span>
            {entry.name}
          </td>
          <td className="size">{entry.is_dir ? "" : formatSize(entry.size)}</td>
          <td className="mtime">{formatMtime(entry.mtime)}</td>
        </tr>
      );
    });
    body = rows.length ? (
      rows
    ) : (
      <tr>
        <td colSpan={3} className="status-message">
          Empty directory
        </td>
      </tr>
    );
  }

  // --- search match count (inline in the search row) ------------------------

  let searchCount: string | null = null;
  if (searching && walk.status === "ok" && hits.length > 0) {
    searchCount =
      hits.length > MAX_RESULTS
        ? `showing ${MAX_RESULTS} of ${hits.length} matches`
        : `${hits.length} match${hits.length === 1 ? "" : "es"}`;
  }

  return (
    <div className="listing">
      <div className="listing-search">
        <input
          type="search"
          className="listing-search-input"
          placeholder="Search this folder…"
          value={query}
          onFocus={prefetchWalk}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              setQuery("");
              e.currentTarget.blur();
            }
          }}
        />
        <button
          className={"search-fuzzy-toggle listing-search-fuzzy-toggle" + (fuzzyOn ? " active" : "")}
          title="Fuzzy matching on/off"
          onClick={() => setFuzzyEnabled(!fuzzyOn)}
        >
          fuzzy
        </button>
        {searching && (walk.status === "idle" || walk.status === "loading") && (
          <span className="listing-search-spinner" aria-hidden="true" />
        )}
        {searchCount !== null && <span className="listing-search-count">{searchCount}</span>}
      </div>
      <div className={"listing-scroll" + (isStale ? " listing-stale" : "")}>
        <table className="listing-table">
          <thead>
            <tr>
              {(Object.entries(SORT_KEYS) as [SortKey, string][]).map(([key, label]) =>
                searching ? (
                  // While searching, headers sort the results; no active arrow
                  // means relevance (fuzzy-rank) order.
                  <th
                    key={key}
                    className={"sortable" + (searchSort?.sort === key ? " sorted" : "")}
                    onClick={() => setSearchSortKey(key)}
                  >
                    {label}
                    {searchSort?.sort === key && (
                      <span className="sort-arrow">{searchSort.order === "asc" ? "▲" : "▼"}</span>
                    )}
                  </th>
                ) : (
                  <th
                    key={key}
                    className={"sortable" + (key === sort ? " sorted" : "")}
                    onClick={() => setSort(key)}
                  >
                    {label}
                    {key === sort && <span className="sort-arrow">{order === "asc" ? "▲" : "▼"}</span>}
                  </th>
                )
              )}
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </div>
  );
}
