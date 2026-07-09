// Directory listing view with sortable columns and an in-folder search.
// Sort state lives in the URL (?sort=name|size|mtime&order=asc|desc) so a
// sorted listing is refresh-proof and bookmarkable like any other view state;
// the search query rides the URL the same way (?q=…). A non-empty query swaps
// the listing for flat, rank-ordered results over a recursive walk of the
// folder. The walk STREAMS (NDJSON batches, breadth-first from the server):
// results paint from the first batch and refine while deeper levels are still
// arriving, so feedback is instant even on huge trees. The walk starts lazily
// on first focus (or a URL-seeded query) and is cached until the dir watch
// fires.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { navigate } from "../lib/router";
import { listDir, walkDirStream } from "../lib/api";
import type { FsEntry, WalkEntry } from "../lib/api";
import { formatSize, formatMtime } from "../lib/format";
import { fuzzyMatch, highlightSegments } from "../lib/fuzzy";

const SORT_KEYS = { name: "Name", size: "Size", mtime: "Modified" };
type SortKey = keyof typeof SORT_KEYS;
type SortOrder = "asc" | "desc";

// Search-result rows rendered per "page". Fuzzy-scoring can match thousands
// of entries in a large tree; mounting them all as <tr>s at once is what jams
// the main thread (scoring itself is comparatively cheap). Scrolling to the
// bottom reveals the next page (see the sentinel row below); the full ranked
// list always exists in memory for the count text.
const PAGE_SIZE = 250;

// Debounce for mirroring the query into the URL. Safari rate-limits
// history.replaceState (~100 calls / 30s, then it THROWS); per-keystroke
// sync trips that on fast typing. State stays immediate — only the URL lags.
const URL_SYNC_MS = 200;

// Minimum gap between streaming state flushes. Network chunks can arrive many
// times per second on localhost; committing (and re-scoring) on every one
// saturates the main thread and starves interaction. The first batch still
// flushes immediately (lastFlush starts at 0), so first paint isn't delayed.
const STREAM_FLUSH_MS = 200;

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

// A dot-leading query segment is explicit intent to SEE hidden entries.
// The walk itself always includes hidden entries (one dataset — the server
// prunes the actually-heavy machine trees like .git/node_modules, so hidden
// files are cheap to carry); this only gates whether dot-entries are shown.
// That makes ".py" work as an extension search (dotfiles like .pylintrc may
// match too — fine, they're real matches) without a second walk, and "env"
// deliberately not surface ".env".
function queryWantsHidden(rawQuery: string): boolean {
  const q = rawQuery.trim();
  return q.startsWith(".") || q.includes("/.");
}

// An entry is hidden when any path segment is dot-leading.
function isHiddenRel(rel: string): boolean {
  return rel.startsWith(".") || rel.includes("/.");
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

// Ranking: longest consecutive matched run first (a contiguous substring hit
// always beats a scattered subsequence one), then higher fuzzy score, then
// fewer path segments (shallower = closer to hand), then alphabetical for a
// stable order. Hits keep their score fields so partial result sets can be
// merged and re-sorted incrementally as the walk streams in.
interface SearchHit {
  entry: WalkEntry;
  positions: number[];
  score: number;
  longestRun: number;
}

function rankCompare(a: SearchHit, b: SearchHit): number {
  if (b.longestRun !== a.longestRun) return b.longestRun - a.longestRun;
  if (b.score !== a.score) return b.score - a.score;
  const ad = a.entry.rel.split("/").length;
  const bd = b.entry.rel.split("/").length;
  if (ad !== bd) return ad - bd;
  return a.entry.rel.localeCompare(b.entry.rel, undefined, { sensitivity: "base" });
}

// Score `entries[from..]` against the query (unsorted — callers sort with
// rankCompare after merging). `showHidden=false` skips dot-entries before
// scoring (see queryWantsHidden). The `from` offset is what makes streaming
// cheap: each flush scores only the entries that arrived since the last one.
function scoreEntries(query: string, entries: WalkEntry[], from: number, showHidden: boolean): SearchHit[] {
  const hits: SearchHit[] = [];
  for (let i = from; i < entries.length; i++) {
    const entry = entries[i];
    if (!showHidden && isHiddenRel(entry.rel)) continue;
    const m = fuzzyMatch(query, entry.rel);
    if (m) hits.push({ entry, positions: m.positions, score: m.score, longestRun: m.longestRun });
  }
  return hits;
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

// Streamed walk state. `entries` is one append-only array shared across the
// streaming updates (each batch pushes into it); every update still creates a
// NEW state object, so React re-renders and memos keyed on `walk` recompute
// against the grown array. `count` is the running total (doubles as the
// version stamp that makes successive streaming states distinguishable).
// There is no per-generation tagging here: the component remounts per folder
// (keyed on fsPath in App), and the fetch effect below aborts + restarts on a
// refresh bump, so a stale stream can never write into the current state —
// at worst one frame renders results from the pre-refresh tree, which is the
// same folder a moment earlier.
type WalkState =
  | { status: "idle" }
  | { status: "streaming"; entries: WalkEntry[]; count: number }
  | { status: "ok"; entries: WalkEntry[]; truncated: boolean; total: number }
  | { status: "error"; message: string };

export default function Listing({ fsPath }: { fsPath: string }) {
  const [state, setState] = useState<ListingState>({ status: "loading" });
  // Sort lives in the URL; mirror it in state so clicks re-render without a
  // navigation (vanilla re-ran renderListing after its replaceState).
  const [{ sort, order }, setSortState] = useState<{ sort: SortKey; order: SortOrder }>(currentSort);
  const [refresh, setRefresh] = useState(0); // bumped by the dir watch socket
  const [query, setQueryState] = useState<string>(currentQuery);
  const [walk, setWalk] = useState<WalkState>({ status: "idle" });
  // Whether the walk should exist at all: flips true on first focus or first
  // typed/URL-seeded query, and stays true — the stream effect below keys on
  // it (plus refresh) to fetch. Never reset: navigation remounts the view.
  const [walkWanted, setWalkWanted] = useState<boolean>(() => currentQuery().trim() !== "");
  // Bumped to re-run the stream effect after an error, from a real user
  // gesture only (focus / typing) — an effect-driven retry would loop forever.
  const [retryNonce, setRetryNonce] = useState(0);
  // Sort applied to search results. null = relevance (fuzzy rank). Deliberately
  // NOT URL-synced (unlike the normal-mode sort) — it resets on every query
  // change, so persisting it would fight that reset.
  const [searchSort, setSearchSort] = useState<{ sort: SortKey; order: SortOrder } | null>(null);
  // How many result rows are revealed; grows by PAGE_SIZE when the sentinel
  // row scrolls into view, resets on every query change.
  const [visibleCount, setVisibleCount] = useState(PAGE_SIZE);

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

  // The streamed walk. One effect owns the whole fetch lifecycle: it starts
  // when the walk is first wanted, re-runs when the dir watch bumps `refresh`
  // (fresh tree) or a gesture bumps `retryNonce` after an error, and ABORTS
  // the in-flight stream on cleanup — which also cancels the server-side
  // walk (the generator is closed on disconnect). Batches push into one
  // append-only array; see WalkState above.
  useEffect(() => {
    if (!walkWanted) return;
    const ctrl = new AbortController();
    let alive = true;
    const entries: WalkEntry[] = [];
    // Flush throttle (STREAM_FLUSH_MS): entries accumulate in `pending`
    // between commits so the scoring/render work runs a few times a second,
    // not once per network chunk. A trailing timer guarantees the last
    // partial interval still commits.
    let pending: WalkEntry[] = [];
    let lastFlush = 0;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    const flush = () => {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      for (const e of pending) entries.push(e); // no spread: a big chunk would blow the arg limit
      pending = [];
      lastFlush = Date.now();
      setWalk({ status: "streaming", entries, count: entries.length });
    };
    setWalk({ status: "streaming", entries, count: 0 });
    walkDirStream(fsPath, {
      hidden: true,
      signal: ctrl.signal,
      onBatch: (batch) => {
        if (!alive) return;
        for (const e of batch) pending.push(e);
        const wait = STREAM_FLUSH_MS - (Date.now() - lastFlush);
        if (wait <= 0) flush();
        else if (flushTimer === null) flushTimer = setTimeout(() => alive && flush(), wait);
      },
    }).then(
      (end) => {
        if (!alive) return;
        if (flushTimer !== null) clearTimeout(flushTimer);
        for (const e of pending) entries.push(e);
        setWalk({ status: "ok", entries, truncated: end.truncated, total: end.total });
      },
      (err: Error) => {
        if (!alive || err.name === "AbortError") return;
        if (flushTimer !== null) clearTimeout(flushTimer);
        setWalk({ status: "error", message: err.message });
      }
    );
    return () => {
      alive = false;
      if (flushTimer !== null) clearTimeout(flushTimer);
      ctrl.abort();
    };
  }, [fsPath, refresh, walkWanted, retryNonce]);

  // First focus starts the walk warming in the background; focus (like
  // typing below) is also the retry gesture when a previous stream failed.
  const prefetchWalk = () => {
    setWalkWanted(true);
    if (walk.status === "error") setRetryNonce((n) => n + 1);
  };

  // Debounced URL mirror for the query (see URL_SYNC_MS). Pending sync is
  // dropped on unmount — a navigation has already replaced the URL by then.
  const urlTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    },
    []
  );

  const setQuery = (value: string) => {
    setQueryState(value);
    setSearchSort(null); // a new query drops back to relevance order
    setVisibleCount(PAGE_SIZE);
    if (value.trim() !== "") setWalkWanted(true);
    // Editing the query is also a user gesture: if the last walk attempt
    // failed, give it another shot instead of leaving search dead forever.
    if (walk.status === "error") setRetryNonce((n) => n + 1);
    if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    urlTimer.current = setTimeout(() => {
      const params = new URLSearchParams(location.search);
      if (value) params.set("q", value);
      else params.delete("q");
      const qs = params.toString();
      history.replaceState(null, "", location.pathname + (qs ? "?" + qs : ""));
    }, URL_SYNC_MS);
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

  // Incremental-scoring cache for the streamed walk. As long as the query,
  // hidden-intent and entries array are unchanged, only entries appended
  // since `scored` get fuzzy-matched, then merged into the previous ranked
  // list — so a stream flush near the tail of a 200k walk costs one small
  // scan + a sort of the hits, not a full re-scan of everything (which is
  // exactly what saturated the main thread and made the UI unresponsive
  // while the walk loaded). Any change to query/hidden/array falls back to a
  // full scan. A ref (not state): it's a pure memo accelerator, and the
  // update below is idempotent, so double-invoked renders are harmless.
  const scoreCache = useRef<{
    q: string;
    showHidden: boolean;
    entries: WalkEntry[] | null;
    scored: number; // how many of `entries` have been scored already
    ranked: SearchHit[];
  }>({ q: "", showHidden: false, entries: null, scored: 0, ranked: [] });

  // Keyed on `q`/`searching` (both deferred) so full fuzzy scans run on
  // React's low-priority schedule, not synchronously on every keystroke.
  // While the walk streams, each flush produces a new `walk` state and this
  // extends the ranked list with just the newly arrived entries (see
  // scoreCache above).
  const hits = useMemo(() => {
    if (!searching || (walk.status !== "ok" && walk.status !== "streaming")) return [];
    const showHidden = queryWantsHidden(q);
    const cache = scoreCache.current;
    let ranked: SearchHit[];
    if (cache.entries === walk.entries && cache.q === q && cache.showHidden === showHidden) {
      const fresh = scoreEntries(q, walk.entries, cache.scored, showHidden);
      ranked = fresh.length ? cache.ranked.concat(fresh).sort(rankCompare) : cache.ranked;
    } else {
      ranked = scoreEntries(q, walk.entries, 0, showHidden).sort(rankCompare);
    }
    scoreCache.current = { q, showHidden, entries: walk.entries, scored: walk.entries.length, ranked };
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
  }, [searching, q, walk, searchSort]);

  const visibleHits = useMemo(() => hits.slice(0, visibleCount), [hits, visibleCount]);

  // Reveal the next page when the sentinel row (rendered only while more rows
  // exist) scrolls into view. rootMargin pre-triggers a bit before the bottom
  // so the next page is usually mounted by the time the user reaches it.
  const sentinelRef = useRef<HTMLTableRowElement | null>(null);
  const hasMore = searching && hits.length > visibleCount;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!el || !hasMore) return;
    const io = new IntersectionObserver(
      (obsEntries) => {
        if (obsEntries.some((e) => e.isIntersecting)) setVisibleCount((c) => c + PAGE_SIZE);
      },
      { root: el.closest(".listing-scroll"), rootMargin: "200px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [hasMore, visibleCount]);

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
    } else if (walk.status === "ok" || walk.status === "streaming") {
      if (hits.length) {
        body = (
          <>
            {visibleHits.map(({ entry, positions }) => {
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
            })}
            {hasMore && (
              <tr ref={sentinelRef}>
                <td colSpan={3} className="status-message">
                  Scroll for more…
                </td>
              </tr>
            )}
          </>
        );
      } else {
        // No matches. Say so honestly: distinguish "still looking" (stream
        // running) and "the walk didn't even cover everything" (truncated) —
        // the old UI showed a bare "No matches" even when the file existed
        // in a region the capped walk never reached.
        const message =
          walk.status === "streaming"
            ? `No matches yet — still searching (${walk.count.toLocaleString()} entries scanned)`
            : walk.truncated
            ? `No matches in the first ${walk.total.toLocaleString()} entries — this folder tree is too large to search fully`
            : "No matches";
        body = (
          <tr>
            <td colSpan={3} className="status-message">
              {message}
            </td>
          </tr>
        );
      }
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
  if (searching && walk.status === "streaming") {
    // Live progress while the walk streams: match count so far + how much of
    // the tree has been scanned. Updates in place, no layout shift.
    searchCount = `${hits.length.toLocaleString()} match${hits.length === 1 ? "" : "es"} · ${walk.count.toLocaleString()} scanned…`;
  } else if (searching && walk.status === "ok" && hits.length > 0) {
    // A truncated walk (server safety cap) means `hits` undercounts the real
    // tree. Signal that without new UI: a "+" on the number plus a tooltip.
    const suffix = walk.truncated ? "+" : "";
    searchCount = `${hits.length.toLocaleString()}${suffix} match${hits.length === 1 ? "" : "es"}`;
    if (walk.truncated)
      searchCountTitle = `Search covers the first ${walk.total.toLocaleString()} entries of this folder tree`;
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
        {searching && (walk.status === "idle" || walk.status === "streaming") && (
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
