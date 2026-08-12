// The in-folder search: query state (URL-synced), the streamed recursive walk,
// incremental fuzzy scoring, the render-side re-rank throttle, the
// stale-while-revalidate hold across dir-watch refreshes, and the result cap.
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
import { nextHeldCorpus, scannableCorpus, type HeldCorpus } from "@apps/explorer/listing/corpus-hold";
import {
  fsMutationCount,
  indexLifecycleCount,
  indexMayAnswer,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { replaceSearch } from "@platform/lib/router";
import { nextHeldHits, resolveDisplayedHits, type QueryTagged } from "@platform/lib/search-hold";
import { useRankedScan } from "@apps/explorer/listing/useRankedScan";
import { shouldReconcile } from "@apps/explorer/listing/revalidate";
import { capHits } from "@apps/explorer/listing/result-cap";
import {
  IDLE_WALK,
  RERANK_COMMIT_MS,
  SCAN_DEBOUNCE_MS,
  STREAM_FLUSH_MS,
  URL_SYNC_MS,
  type SearchHit,
  type WalkState,
} from "@apps/explorer/listing/types";

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

  // The index being deleted or a scan completing invalidates the fetched
  // corpus the same way a dir-watch bump does, and needs its own signal: the
  // filesystem didn't change, so no watch refresh will ever re-key the fetch
  // (lib/index-freshness). Composed into `gen` so it rides the SAME deferral —
  // adopted at the boundaries while a search is on screen, immediately
  // otherwise — rather than swapping results mid-read.
  const [lifecycle, setLifecycle] = useState(indexLifecycleCount);
  useEffect(() => subscribeIndexLifecycle(() => setLifecycle(indexLifecycleCount())), []);
  // The generation this hook answers for: dir-watch refreshes plus index
  // lifecycle events, one monotonic counter.
  const gen = refresh + lifecycle;

  // The generation the SEARCH is pinned to. Outside search it tracks `gen`
  // exactly; during one it lags deliberately, so background churn under the
  // folder cannot dim the user's results mid-read (see listing/revalidate).
  // Everything below keys on this, never on `gen` itself.
  const [pinned, setPinned] = useState(gen);
  // In-app mutations override the deferral — the user's own rename has to show.
  const [mutations, setMutations] = useState(fsMutationCount);
  const appliedMutations = useRef(mutations);
  useEffect(() => subscribeFsMutations(() => setMutations(fsMutationCount())), []);

  const reconcile = () => {
    appliedMutations.current = fsMutationCount();
    setMutations(appliedMutations.current);
    setPinned(gen);
  };
  useEffect(() => {
    if (shouldReconcile({ refresh: gen, pinned, searching, mutations,
                          appliedMutations: appliedMutations.current })) {
      reconcile();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- reconcile is
    // recreated each render; the inputs it reads are all listed here.
  }, [gen, pinned, searching, mutations]);

  // Synchronous cache validity: a non-idle walk fetched for a previous
  // generation reads as idle, immediately on the render where `pinned` moves —
  // no effect ordering to wait on, and no render ever scores search results
  // against the pre-refresh tree.
  const validWalk: WalkState =
    walk.status === "idle" || walk.forRefresh === pinned ? walk : IDLE_WALK;

  // Active search must always have a walk for the CURRENT tree. Covers a
  // URL-seeded query on mount racing ahead of focus, typing after an
  // invalidation, and the dir watch bumping `refresh` mid-search (the stale
  // tag makes validWalk idle, this re-requests). Keyed on validWalk being
  // IDLE so an errored walk never auto-retries (that would loop:
  // request -> error -> request -> ...); error retries hang off real
  // gestures (focus / typing) below. The immediate `query` (not deferred)
  // drives this — the fetch should start on the first keystroke.
  useEffect(() => {
    if (query.trim() !== "" && validWalk.status === "idle" && walkReq !== pinned) {
      setWalkReq(pinned);
    }
  }, [query, validWalk.status, walkReq, pinned]);

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
    // Corpus identities for the two sources (WalkState.key). Distinct per
    // source as well as per generation: both may answer for one generation
    // (they race below), and their entry lists are not the same rows.
    const walkKey = `walk:${fsPath}:${forRefresh}`;
    const indexKey = `index:${fsPath}:${forRefresh}`;
    const flush = () => {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      for (const e of pending) entries.push(e); // no spread: a big chunk would blow the arg limit
      pending = [];
      lastFlush = Date.now();
      setWalk({ status: "streaming", entries, count: entries.length, key: walkKey, forRefresh });
    };
    // Published BEFORE either source has answered, so search reads as "in
    // flight" from this instant. It is deliberately empty rather than
    // preserving the old rows: what stands in meanwhile is the HELD corpus
    // (listing/corpus-hold), which keeps that stand-in explicitly marked stale
    // instead of letting an unsettled state quietly carry last generation's
    // rows under a fresh tag.
    setWalk({ status: "streaming", entries, count: 0, key: walkKey, forRefresh });
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
          setWalk({
            status: "ok",
            entries,
            truncated: end.truncated,
            total: end.total,
            key: walkKey,
            forRefresh,
          });
        },
        (err: Error) => {
          if (!alive || err.name === "AbortError") return;
          if (flushTimer !== null) clearTimeout(flushTimer);
          setWalk({ status: "error", message: err.message, key: walkKey, forRefresh });
        }
      );
    // A folder this app has already marked dirty is decided before any
    // request: the walk is what will answer anyway, so waiting out the index
    // round-trip (plus its gitignore filter) only delays the first results.
    // The same check repeats at resolution time below for the race where the
    // mutation lands while the fetch is in flight.
    if (!indexMayAnswer(fsPath)) {
      void liveWalk();
      return () => {
        alive = false;
        if (flushTimer !== null) clearTimeout(flushTimer);
        ctrl.abort();
      };
    }
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
          key: indexKey,
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

  // First focus starts the walk warming in the background; focus (like typing
  // below) is also the retry gesture when a previous stream failed.
  //
  // Focus is deliberately NOT a revalidation boundary. It reads like one — the
  // user is "coming back to" the search — but it is ambient: the pane focus
  // guard, a split remount at a width threshold, and WebKit restoring focus
  // after a repaint all fire it with no gesture behind them. Treating it as a
  // boundary adopted whatever churn had accumulated and swapped the results
  // out from under someone who was reading them, which is the exact thing the
  // deferral exists to prevent. Requests are tagged `pinned`, never `refresh`,
  // so warming a walk here cannot smuggle a newer generation in either.
  const prefetchWalk = () => {
    if (validWalk.status === "idle") setWalkReq(pinned);
    else if (validWalk.status === "error") {
      setWalkReq(pinned);
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
    // A query change is a boundary: the rows are being replaced anyway, so a
    // generation deferred during the previous query lands here for free.
    reconcile();
    // Editing the query is also a user gesture: if the last walk attempt
    // failed, give it another shot instead of leaving search dead forever.
    // (An idle walk needs no handling here — the auto-request effect fires
    // as soon as the non-empty query state lands.)
    if (validWalk.status === "error") {
      // `gen`, not `pinned`: the reconcile above is setting pinned to it,
      // and that state has not landed yet inside this handler.
      setWalkReq(gen);
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

  // The corpus to rank. The previous generation's entries stay scannable while
  // the next one is fetched, so a keystroke landing mid-refetch ranks against a
  // one-generation-stale corpus and paints at once instead of ranking against
  // an empty array and blanking (listing/corpus-hold). The hold is captured
  // from `validWalk` during render, exactly like the ranked-hit hold below —
  // by the render where `pinned` moves and validWalk reads idle, this ref is
  // already carrying the corpus from the render before it.
  const heldCorpus = useRef<HeldCorpus | null>(null);
  heldCorpus.current = nextHeldCorpus(validWalk, heldCorpus.current);
  const corpus = scannableCorpus(searching, validWalk, heldCorpus.current);

  // The scan itself — incremental, sliced and cancellable, shared with the
  // explorer home page's box (listing/useRankedScan carries the reasoning).
  const { ranked, pending } = useRankedScan(corpus.entries, q, SCAN_DEBOUNCE_MS);

  // What the rest of the hook consumes.
  // Relevance (the fuzzy rank) is the only order search results have. Column
  // sorting used to be offered here and was withdrawn with the Size/Modified
  // headers: the hit set is capped and, mid-walk, partial — a by-date or
  // by-size ordering over it reads as an answer to a question the data cannot
  // answer, and the search UI already warns the coverage is approximate.
  const hits = useMemo(() => (searching ? ranked : []), [searching, ranked]);

  // True while the scan for the current query has not published yet — the
  // spinner and the dimmed-rows treatment key off this, so it has to mean
  // "an answer is still coming", not merely "the deferred value lags".
  const scanPending = searching && pending;
  // `corpus.stale` joins the two pre-existing reasons: rows ranked against the
  // previous generation's corpus are exactly as provisional as rows the scan
  // has not finished producing, and both must read as such.
  const isStale = deferredStale || scanPending || corpus.stale;

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
  // query change is a direct response to a gesture and must paint immediately,
  // never wait out a throttle window opened by the stream.
  useEffect(() => {
    lastRankCommit.current = 0;
  }, [q]);
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

  // The rendered rows: the top of the ranking only (listing/result-cap). This
  // is also what keyboard nav and auto-select walk, so they never address a
  // row that is not on screen.
  const visibleHits = useMemo(() => capHits(displayHits), [displayHits]);

  // How many ranked matches the cap is hiding — the counter reports the true
  // total and tells the user to narrow the query (listing/result-cap). There
  // is deliberately no "load more": scrolling further is the wrong answer.
  const cappedAway = displayHits.length - visibleHits.length;

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
    cappedAway,
  };
}
