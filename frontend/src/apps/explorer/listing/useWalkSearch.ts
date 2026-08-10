// The in-folder search: query state (URL-synced), the streamed recursive walk,
// incremental fuzzy scoring, the render-side re-rank throttle, the
// stale-while-revalidate hold across dir-watch refreshes, and result paging.
//
// A non-empty query swaps the listing for flat, rank-ordered results over a
// recursive walk of the folder. The walk STREAMS (NDJSON batches,
// breadth-first from the server): results paint from the first batch and
// refine while deeper levels are still arriving, so feedback is instant even
// on huge trees. The walk starts lazily on first focus (or a URL-seeded
// query) and is cached until the dir watch fires.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { indexSearch, walkDirStream } from "@platform/lib/api";
import type { WalkEntry } from "@platform/lib/api";
import { indexCorpusFrom } from "@apps/explorer/listing/index-corpus";
import { indexMayAnswer } from "@platform/lib/index-freshness";
import { replaceSearch } from "@platform/lib/router";
import { nextHeldHits, resolveDisplayedHits, type QueryTagged } from "@platform/lib/search-hold";
import { startScanJob } from "@apps/explorer/listing/scan-job";
import {
  IDLE_WALK,
  PAGE_SIZE,
  RERANK_COMMIT_MS,
  SCAN_DEBOUNCE_MS,
  SCAN_IMMEDIATE_MAX,
  SCAN_SLICE,
  STREAM_FLUSH_MS,
  URL_SYNC_MS,
  type SearchHit,
  type SortKey,
  type SortOrder,
  type WalkState,
} from "@apps/explorer/listing/types";
import { queryWantsHidden, rankCompare, scoreEntries, sortHits } from "@apps/explorer/listing/search";

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
}

// `urlSync=false` (an embedded Listing, e.g. the preview pane's `_listing`
// mode) keeps the query fully local: it neither seeds from ?q nor mirrors
// keystrokes back to the address bar — that URL belongs to the host view.
export function useWalkSearch(fsPath: string, refresh: number, urlSync = true) {
  const [query, setQueryState] = useState<string>(() => (urlSync ? currentQuery() : ""));
  const [walk, setWalk] = useState<WalkState>(IDLE_WALK);
  // Which refresh generation of the walk has been REQUESTED (null = none).
  // The fetch effect keys on this, not on `refresh` itself: a dir-watch bump
  // only invalidates the cache (via the forRefresh tag) and a new fetch
  // happens only while search is active (auto-request effect) or on the next
  // gesture — an idle listing must not re-walk the tree on every watch event.
  const [walkReq, setWalkReq] = useState<number | null>(() =>
    urlSync && currentQuery().trim() !== "" ? 0 : null
  );
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
  // `isStale` is completed below, once the scan's own pending state is known:
  // the input can have settled while the chunked scan for it is still running.
  const deferredStale = query.trim() !== q;

  // Synchronous cache validity: a non-idle walk fetched for a previous
  // refresh generation reads as idle, immediately on the render where
  // `refresh` bumps — no effect ordering to wait on, and no render ever
  // scores search results against the pre-refresh tree.
  const validWalk: WalkState =
    walk.status === "idle" || walk.forRefresh === refresh ? walk : IDLE_WALK;

  // Active search must always have a walk for the CURRENT tree. Covers a
  // URL-seeded query on mount racing ahead of focus, typing after an
  // invalidation, and the dir watch bumping `refresh` mid-search (the stale
  // tag makes validWalk idle, this re-requests). Keyed on validWalk being
  // IDLE so an errored walk never auto-retries (that would loop:
  // request -> error -> request -> ...); error retries hang off real
  // gestures (focus / typing) below. The immediate `query` (not deferred)
  // drives this — the fetch should start on the first keystroke.
  useEffect(() => {
    if (query.trim() !== "" && validWalk.status === "idle" && walkReq !== refresh) {
      setWalkReq(refresh);
    }
  }, [query, validWalk.status, walkReq, refresh]);

  // The corpus. One effect owns the whole fetch lifecycle: it runs when a walk
  // generation is requested (walkReq) or a gesture bumps `retryNonce` after an
  // error, and ABORTS the in-flight work on cleanup — which also cancels the
  // server-side walk (the generator is closed on disconnect). Batches push into
  // one append-only array; see WalkState.
  //
  // Two sources, one shape. The persistent file index answers instantly and
  // cross-session when it covers this folder and is fresh (see index-corpus);
  // otherwise — no index yet, first-boot scan still running, folder outside the
  // scanned roots, index gone stale, or the request simply failed — the live
  // streamed walk runs exactly as it always did. The fallback is silent by
  // design: none of those is an error the user can act on, and every one of
  // them is normal in the seconds after a first launch.
  useEffect(() => {
    if (walkReq === null) return;
    const forRefresh = walkReq;
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
      setWalk({ status: "streaming", entries, count: entries.length, forRefresh });
    };
    setWalk({ status: "streaming", entries, count: 0, forRefresh });
    const liveWalk = () =>
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
          setWalk({ status: "ok", entries, truncated: end.truncated, total: end.total, forRefresh });
        },
        (err: Error) => {
          if (!alive || err.name === "AbortError") return;
          if (flushTimer !== null) clearTimeout(flushTimer);
          setWalk({ status: "error", message: err.message, forRefresh });
        }
      );
    indexSearch(fsPath, { signal: ctrl.signal }).then(
      (res) => {
        if (!alive) return;
        // A folder this app has changed since the last scan is walked live:
        // the corpus predates the change, so it would offer the old name and
        // never the new one (lib/index-freshness). Out-of-band edits keep the
        // documented trade — an instant, mostly-right answer.
        const corpus = indexMayAnswer(fsPath) ? indexCorpusFrom(res) : null;
        if (!corpus) {
          void liveWalk();
          return;
        }
        for (const e of corpus.entries) entries.push(e);
        setWalk({
          status: "ok",
          entries,
          truncated: corpus.truncated,
          total: entries.length,
          forRefresh,
        });
      },
      (err: Error) => {
        // Includes the abort case: a cleanup aborts BOTH requests, and
        // liveWalk would immediately abort too, so the guard covers it.
        if (!alive || err.name === "AbortError") return;
        void liveWalk();
      }
    );
    return () => {
      alive = false;
      if (flushTimer !== null) clearTimeout(flushTimer);
      ctrl.abort();
    };
  }, [fsPath, walkReq, retryNonce]);

  // First focus starts the walk warming in the background; focus (like
  // typing below) is also the retry gesture when a previous stream failed.
  const prefetchWalk = () => {
    if (validWalk.status === "idle") setWalkReq(refresh);
    else if (validWalk.status === "error") {
      setWalkReq(refresh);
      setRetryNonce((n) => n + 1);
    }
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
    // Editing the query is also a user gesture: if the last walk attempt
    // failed, give it another shot instead of leaving search dead forever.
    // (An idle walk needs no handling here — the auto-request effect fires
    // as soon as the non-empty query state lands.)
    if (validWalk.status === "error") {
      setWalkReq(refresh);
      setRetryNonce((n) => n + 1);
    }
    if (!urlSync) return;
    if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    urlTimer.current = setTimeout(() => {
      const params = new URLSearchParams(location.search);
      if (value) params.set("q", value);
      else params.delete("q");
      const qs = params.toString();
      replaceSearch(location.pathname + (qs ? "?" + qs : ""));
    }, URL_SYNC_MS);
  };

  // Search headers cycle asc → desc → relevance. Relevance (the fuzzy rank) is
  // the mode search results are actually FOR, and before this there was no way
  // back to it short of retyping the query — the toggle only ever flipped
  // between two column orders.
  const setSearchSortKey = (key: SortKey) => {
    setSearchSort((prev) => {
      if (!prev || prev.sort !== key) return { sort: key, order: "asc" };
      if (prev.order === "asc") return { sort: key, order: "desc" };
      return null;
    });
  };

  // Incremental-scoring cache for the corpus. As long as the query,
  // hidden-intent and entries array are unchanged, only entries appended
  // since `scored` get fuzzy-matched and merged into the previous ranked
  // list — so a stream flush near the tail of a 200k walk costs one small
  // scan, not a re-scan of everything. Any change to query/hidden/array
  // starts a fresh scan. It also carries a chunked scan's PROGRESS, so a job
  // cancelled mid-flight (by the next stream flush) resumes instead of
  // starting over.
  const scoreCache = useRef<{
    q: string;
    showHidden: boolean;
    entries: WalkEntry[] | null;
    scored: number; // how many of `entries` have been scored already
    ranked: SearchHit[];
  }>({ q: "", showHidden: false, entries: null, scored: 0, ranked: [] });

  // The scan's published output, tagged with the query it was computed for.
  // That tag is the whole reason this is not re-derived at render time: see
  // lib/search-hold, and scan-job's header.
  const [scanned, setScanned] = useState<QueryTagged<SearchHit>>(() => ({ q: "", items: [] }));

  const showHidden = queryWantsHidden(q);
  const corpus =
    searching && (validWalk.status === "ok" || validWalk.status === "streaming")
      ? validWalk.entries
      : null;
  // Stream flushes reuse the same entries ARRAY (it is appended in place), so
  // the array identity cannot tell the job that more arrived — its length can.
  const corpusLen = corpus ? corpus.length : 0;

  // The scan itself. An effect, not a memo: a full scan of an index-backed
  // corpus is far too much work to do synchronously on a keystroke, and a
  // memo cannot be interrupted once it starts (useDeferredValue only buys the
  // cheap echo render before it). scan-job slices it, yields between slices,
  // and is cancelled here the moment the query, hidden-intent or corpus moves.
  useEffect(() => {
    if (corpus === null) {
      setScanned((prev) => (prev.q === q && prev.items.length === 0 ? prev : { q, items: [] }));
      return;
    }
    const cache = scoreCache.current;
    const resumable =
      cache.entries === corpus && cache.q === q && cache.showHidden === showHidden;
    if (!resumable) {
      scoreCache.current = { q, showHidden, entries: corpus, scored: 0, ranked: [] };
    }
    const live = scoreCache.current;
    return startScanJob(
      {
        q,
        showHidden,
        entries: corpus,
        from: live.scored,
        ranked: live.ranked,
        sliceSize: SCAN_SLICE,
        immediateMax: SCAN_IMMEDIATE_MAX,
        debounceMs: SCAN_DEBOUNCE_MS,
        commitMs: RERANK_COMMIT_MS,
      },
      {
        score: scoreEntries,
        sort: (hitsToSort) => hitsToSort.sort(rankCompare),
        now: Date.now,
        setTimer: (fn, ms) => window.setTimeout(fn, ms),
        clearTimer: (id) => window.clearTimeout(id),
        onPublish: (result) => setScanned(result),
        onProgress: (n) => {
          live.scored = n;
        },
      },
    );
  }, [q, showHidden, corpus, corpusLen]);

  // What the rest of the hook consumes. Rows are dropped unless they were
  // computed for the CURRENT query, so a scan still catching up never shows
  // the previous query's matches — the same rule search-hold enforces
  // downstream, applied at the source.
  const hits = useMemo(() => {
    if (!searching || scanned.q !== q) return [];
    if (!searchSort) return scanned.items; // relevance order
    return sortHits(scanned.items, searchSort.sort, searchSort.order);
  }, [searching, scanned, q, searchSort]);

  // True while the scan for the current query has not published yet — the
  // spinner and the dimmed-rows treatment key off this, so it has to mean
  // "an answer is still coming", not merely "the deferred value lags".
  const scanPending = searching && scanned.q !== q;
  const isStale = deferredStale || scanPending;

  // --- Streaming re-rank throttle (B4) --------------------------------------
  // Every stream flush re-scores the newly arrived entries and merges them into
  // the ranked list, which reshuffles the visible rows several times a second —
  // unreadable on its own, and worse now that the rows animate. The RENDERED
  // ranking is therefore committed at most once per RERANK_COMMIT_MS. The
  // accumulation underneath and the live "N matches · M scanned…" counter both
  // keep running at full speed: they cost nothing and they are the honest
  // progress signal.
  //
  // The committed ranking carries the QUERY it was computed for. That tag is
  // load-bearing for the hold below and it has to be committed WITH the data:
  // on the first render after `q` changes this state still holds the previous
  // query's rows (this effect hasn't run yet), so anything that tags it at
  // render time labels the old query's rows with the new query. See
  // lib/search-hold.
  const [rankedForRender, setRankedForRender] = useState<QueryTagged<SearchHit>>(() => ({
    q,
    items: hits,
  }));
  const lastRankCommit = useRef(0);
  // Declared BEFORE the commit effect so it runs first in the same flush: a
  // query change (or a sort change) is a direct response to a gesture and must
  // paint immediately, never wait out a throttle window opened by the stream.
  useEffect(() => {
    lastRankCommit.current = 0;
  }, [q, searchSort]);
  useEffect(() => {
    // Only a streaming walk churns. Anything else (settled, errored, or
    // invalidated to idle) commits at once — there is nothing left to smooth
    // and the final ranking must not be held back.
    if (validWalk.status !== "streaming") {
      lastRankCommit.current = 0;
      setRankedForRender({ q, items: hits });
      return;
    }
    const wait = RERANK_COMMIT_MS - (Date.now() - lastRankCommit.current);
    const commit = () => {
      lastRankCommit.current = Date.now();
      setRankedForRender({ q, items: hits });
    };
    if (wait <= 0) {
      commit(); // includes the first flush of a stream — first paint isn't delayed
      return;
    }
    const id = window.setTimeout(commit, wait);
    return () => window.clearTimeout(id);
  }, [hits, q, validWalk.status]);

  // --- Stale-while-revalidate for search results (B3) -----------------------
  // A dir-watch event bumps `refresh`, which makes validWalk read idle for the
  // stale generation, which collapses `hits` to [] — so the entire visible
  // result list used to blank to "Searching…" while the tree re-walked, for as
  // long as that takes on a big folder. Hold the last ranked answer and keep
  // rendering it (dimmed, with the spinner) until the fresh one is ready.
  //
  // This changes only what is DISPLAYED. The invalidation machinery is
  // untouched: a stale walk is still never scored against a new tree — `hits`
  // remains derived from validWalk alone, and these held rows are not fed back
  // into scoring.
  //
  // Both halves of the decision — what to retain, and what to render — live in
  // lib/search-hold, pure and query-tagged: rows are only ever shown under the
  // query they were computed for, so the ONE thing this must never do (show the
  // previous query's matches under a new query) is structurally impossible
  // rather than a condition someone has to remember. A query change falls
  // through to "Searching…" exactly as before; only a same-query invalidation
  // holds. The hold applies only while the current-generation walk is unsettled
  // — a COMPLETED walk with no hits is a real "no matches" answer (the file was
  // just deleted, say) and replaces the held rows.
  const heldHits = useRef<QueryTagged<SearchHit> | null>(null);
  heldHits.current = nextHeldHits(searching, q, rankedForRender, heldHits.current);
  const walkUnsettled = validWalk.status === "idle" || validWalk.status === "streaming";
  const { hits: displayHits, showingHeld } = resolveDisplayedHits(
    searching,
    q,
    rankedForRender,
    heldHits.current,
    walkUnsettled,
  );

  const visibleHits = useMemo(() => displayHits.slice(0, visibleCount), [displayHits, visibleCount]);

  // Reveal the next page when the sentinel row (rendered only while more rows
  // exist) scrolls into view. rootMargin pre-triggers a bit before the bottom
  // so the next page is usually mounted by the time the user reaches it.
  const sentinelRef = useRef<HTMLTableRowElement | null>(null);
  const hasMore = searching && displayHits.length > visibleCount;
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

  return {
    query,
    setQuery,
    q,
    searching,
    isStale,
    scanPending,
    validWalk,
    prefetchWalk,
    hits,
    displayHits,
    visibleHits,
    showingHeld,
    hasMore,
    sentinelRef,
    searchSort,
    setSearchSort,
    setSearchSortKey,
  };
}
