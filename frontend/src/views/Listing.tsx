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

// Cached recursive walk. Non-idle states are tagged with the (fsPath, refresh)
// they were fetched for; `validWalk` below compares that tag against the
// current fsPath/refresh at render time so a stale walk (folder changed, dir
// watch fired) is treated as idle synchronously — no waiting on an effect to
// clear it, which would otherwise let one render score search results
// against the previous tree.
type WalkState =
  | { status: "idle" }
  | { status: "loading"; forPath: string; forRefresh: number }
  | { status: "ok"; result: WalkResult; forPath: string; forRefresh: number }
  | { status: "error"; message: string; forPath: string; forRefresh: number };

const IDLE_WALK: WalkState = { status: "idle" };

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

  // Synchronous validity check: a non-idle walk only counts if it was fetched
  // for the folder/refresh generation we're currently rendering. This makes
  // invalidation immediate on the render where `refresh` bumps, instead of
  // depending on the clearing effect below to run first (which left one
  // render scoring search results against the stale tree).
  const validWalk: WalkState =
    walk.status === "idle" || (walk.forPath === fsPath && walk.forRefresh === refresh) ? walk : IDLE_WALK;

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
  // "loading" as the trigger keeps the fetch out of a state updater. Gated on
  // validWalk (not the raw status) so a loading state that goes stale
  // mid-flight (fsPath/refresh changed again before it resolved) doesn't
  // have its result tagged as current — the alive-cleanup still discards it,
  // and the effect below will re-request against the new generation.
  useEffect(() => {
    if (validWalk.status !== "loading") return;
    let alive = true;
    const forPath = fsPath;
    const forRefresh = refresh;
    walkDir(fsPath).then(
      (result) => alive && setWalk({ status: "ok", result, forPath, forRefresh }),
      (err: Error) => alive && setWalk({ status: "error", message: err.message, forPath, forRefresh })
    );
    return () => {
      alive = false;
    };
  }, [validWalk.status, fsPath, refresh]);

  // A query restored from the URL needs the walk even without a focus event;
  // this also covers refetch-on-refresh while search is active, since a
  // refresh bump makes validWalk go stale->"idle" synchronously, which this
  // effect (keyed on validWalk.status) picks up immediately. Deliberately
  // keyed off "idle" only (not "error") — retrying "error" here would loop
  // forever (loading -> error -> retry -> loading -> error -> ...) with no
  // user action in between. Error retries are wired to real user gestures
  // instead: prefetchWalk (focus) and setQuery (typing) below.
  useEffect(() => {
    if (searching && validWalk.status === "idle") {
      setWalk({ status: "loading", forPath: fsPath, forRefresh: refresh });
    }
  }, [searching, validWalk.status, fsPath, refresh]);

  // Retry from "idle" (first focus) or "error" (a prior fetch failed) — treat
  // both as "no usable walk cached yet". Called from onFocus, a genuine user
  // gesture, so this can't spin in a loop the way an effect could.
  const prefetchWalk = () => {
    if (validWalk.status === "idle" || validWalk.status === "error") {
      setWalk({ status: "loading", forPath: fsPath, forRefresh: refresh });
    }
  };

  const setQuery = (value: string) => {
    setQueryState(value);
    setSearchSort(null); // a new query drops back to relevance order
    // Editing the query is also a user gesture: if the last walk attempt
    // failed, give it another shot instead of leaving search dead forever.
    if (validWalk.status === "error") {
      setWalk({ status: "loading", forPath: fsPath, forRefresh: refresh });
    }
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
    if (!(searching && validWalk.status === "ok")) return [];
    const matcher = fuzzyOn ? fuzzyMatch : substringMatch;
    const ranked = searchWalk(q, validWalk.result.entries, matcher);
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
  }, [searching, q, validWalk, searchSort, fuzzyOn]);

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
    if (validWalk.status === "error") {
      body = (
        <tr>
          <td colSpan={3} className="status-message error">
            Search failed: {validWalk.message}
          </td>
        </tr>
      );
    } else if (validWalk.status === "ok") {
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
  let searchCountTitle: string | undefined;
  if (searching && validWalk.status === "ok" && hits.length > 0) {
    // A truncated walk (20k-entry server cap) means `hits` undercounts the
    // real tree. Signal that without new UI: a "+" on the number plus a
    // tooltip on the existing chip, rather than separate "truncated" text
    // that would shift layout.
    const truncated = validWalk.result.truncated;
    const suffix = truncated ? "+" : "";
    searchCount =
      hits.length > MAX_RESULTS
        ? `showing ${MAX_RESULTS} of ${hits.length}${suffix} matches`
        : `${hits.length}${suffix} match${hits.length === 1 ? "" : "es"}`;
    if (truncated) searchCountTitle = "Search covers the first 20,000 entries of this folder tree";
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
        {searching && (validWalk.status === "idle" || validWalk.status === "loading") && (
          <span className="listing-search-spinner" aria-hidden="true" />
        )}
        {searchCount !== null && (
          <span className="listing-search-count" title={searchCountTitle}>
            {searchCount}
          </span>
        )}
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
