// The in-folder search: query state (URL-synced), the streamed recursive walk,
// incremental fuzzy scoring, the render-side re-rank throttle, the
// stale-while-revalidate holds, and the result cap.
//
// A non-empty query swaps the listing for flat, rank-ordered results over a
// recursive walk of the folder. The walk STREAMS (NDJSON batches,
// breadth-first from the server): results paint from the first batch and
// refine while deeper levels are still arriving, so feedback is instant even
// on huge trees. The walk starts lazily on first focus (or a URL-seeded query).
//
// The corpus is then KEPT. Neither a dir-watch event nor a scan completing
// re-fetches it: both are recorded and the results stay put, dimmed and
// captioned "not refreshed", until a boundary where a repaint costs the user
// nothing — the search ending, or a change this app itself made. That is the
// deliberate trade this file makes, and it is worth restating because it is
// the opposite of what a cache usually does: a corpus a few minutes old that
// says so beats one that keeps being pulled out from under the reader. See
// listing/revalidate.
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
import { startRace } from "@apps/explorer/listing/source-race";
import {
  IDLE_WALK,
  INDEX_RACE_MS,
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

  // The index being deleted or a scan completing dates the fetched corpus the
  // same way a dir-watch bump does, and needs its own signal: the filesystem
  // didn't change, so no watch refresh will ever re-key the fetch
  // (lib/index-freshness). Composed into `gen` so it rides the SAME deferral,
  // which matters more for this one than for the watch: scans complete often,
  // and treating each as an invalidation is what made search blank mid-read.
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

  // The corpus retained across an invalidation, so a refetch never blanks the
  // rows (listing/corpus-hold). Declared here because the reconcile below has
  // to be able to throw it away.
  const heldCorpus = useRef<HeldCorpus | null>(null);

  const reconcile = () => {
    // A reconcile forced by THIS APP's own mutation must not leave the old
    // corpus standing in for the refetch: it holds the pre-rename name, which
    // is the one thing search must never offer back (lib/index-freshness).
    // Every other reconcile is background churn, where holding is the point.
    if (fsMutationCount() !== appliedMutations.current) heldCorpus.current = null;
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
  //
  // They RACE rather than queue. Awaiting the index in full made it strictly
  // worse than the walk it replaced whenever it was slow (the gitignore sweep
  // after a scan): nothing on screen for seconds, then everything at once,
  // where the walk would have painted its first batch in ~100ms. So the walk
  // starts if the index has not produced within INDEX_RACE_MS, and exactly one
  // of them ends up owning the answer — see listing/source-race for why
  // interleaving them is not an option.
  //
  // Note what this effect does NOT do: run when there is already a corpus. It
  // is driven by `walkReq`, which only moves when `validWalk` reads idle — and
  // background churn no longer makes it read idle (listing/revalidate). So the
  // race, and the second request it can cost, are confined to the case they
  // are for: this folder has nothing to show yet.
  useEffect(() => {
    if (walkReq === null) return;
    const forRefresh = walkReq;
    // One controller per source: the race aborts the LOSER on its own (which
    // closes the server-side walk generator) while the winner keeps running.
    // Cleanup aborts both.
    const ctrl = { index: new AbortController(), walk: new AbortController() };
    const race = startRace((loser) => ctrl[loser].abort());
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
      // Producing is what claims the race, so the FIRST flush is where the walk
      // wins — and where it loses, if the index already answered. Nothing is
      // pushed on a loss: `entries` only ever holds one source's rows.
      if (!race.claim("walk")) return;
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
    // A walk that fails while the index is still in flight is NOT the answer
    // yet — the index may still cover this folder — so its error is remembered
    // and surfaced only once the index has been ruled out. Without that, an
    // early walk failure (the race started it, the folder is unreadable) would
    // paint "Search failed" over an index answer that was one tick away.
    let walkError: Error | null = null;
    let indexPending = false;
    let walkStarted = false;
    let raceTimer: ReturnType<typeof setTimeout> | null = null;
    const startWalk = () => {
      if (walkStarted) return;
      walkStarted = true;
      void walkDirStream(fsPath, {
        hidden: true,
        signal: ctrl.walk.signal,
        onBatch: (batch) => {
          if (!alive || race.winner() === "index") return;
          for (const e of batch) pending.push(e);
          const wait = STREAM_FLUSH_MS - (Date.now() - lastFlush);
          if (wait <= 0) flush();
          else if (flushTimer === null) flushTimer = setTimeout(() => alive && flush(), wait);
        },
      }).then(
        (end) => {
          if (!alive) return;
          if (flushTimer !== null) clearTimeout(flushTimer);
          if (!race.claim("walk")) return;
          for (const e of pending) entries.push(e);
          pending = [];
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
          if (race.winner() === "index") return;
          walkError = err;
          if (indexPending) return; // the index still might answer
          race.claim("walk");
          setWalk({ status: "error", message: err.message, key: walkKey, forRefresh });
        }
      );
    };
    // The index has nothing for this folder (uncovered, dirty, or the request
    // failed). Whatever the walk is doing is now the only answer there will be.
    const indexIsOut = () => {
      if (race.winner() === "walk") return; // it already produced; nothing to do
      if (walkError !== null) {
        race.claim("walk");
        setWalk({ status: "error", message: walkError.message, key: walkKey, forRefresh });
        return;
      }
      startWalk();
    };
    // A folder this app has already marked dirty is decided before any
    // request, and the walk race above does NOT soften that.
    //
    // The race was the reason to revisit this gate: with the walk running
    // anyway, refusing the index looks like it only costs latency. It does
    // not. The index is not slower here, it is WRONG — its corpus predates
    // the rename, so it would answer with the old name — and it would win the
    // race handily, because answering from a corpus already on disk is exactly
    // what it is fast at. A race fixes a slow answer, never a false one.
    //
    // Narrowing the gate to the mutated folder itself is not available either:
    // in-folder search is recursive, so a rename anywhere below `fsPath`
    // poisons its corpus, and a renamed ANCESTOR moves every path inside it.
    // Both directions of lib/index-freshness's check are load-bearing (its
    // test file pins them). The documented cost stands: one rename pins this
    // folder and its ancestors to the live walk for the session, which the
    // home page rightly refuses to pay because it has no walk to fall back
    // on (see FilesHome) and which this box can afford because it does.
    if (!indexMayAnswer(fsPath)) {
      startWalk();
    } else {
      indexPending = true;
      // The budget. If the index has produced nothing by now, stop waiting on
      // it and let the walk stream while it finishes (listing/source-race).
      raceTimer = setTimeout(() => {
        if (alive && !race.claimed()) startWalk();
      }, INDEX_RACE_MS);
      indexSearch(fsPath, { signal: ctrl.index.signal }).then(
        (res) => {
          if (!alive) return;
          indexPending = false;
          // A folder this app has changed since the last scan is walked live:
          // the corpus predates the change, so it would offer the old name and
          // never the new one (lib/index-freshness). Re-checked here for the
          // race where the mutation lands while the fetch is in flight.
          // Out-of-band edits keep the documented trade — an instant,
          // mostly-right answer.
          const corpus = indexMayAnswer(fsPath) ? indexCorpusFrom(res) : null;
          if (!corpus) {
            indexIsOut();
            return;
          }
          if (!race.claim("index")) return; // the walk beat it; its rows stand
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
          if (!alive) return;
          indexPending = false;
          // An abort here is either the cleanup or the race cancelling the
          // loser; neither is a reason to start anything.
          if (err.name === "AbortError") return;
          indexIsOut();
        }
      );
    }
    return () => {
      alive = false;
      if (flushTimer !== null) clearTimeout(flushTimer);
      if (raceTimer !== null) clearTimeout(raceTimer);
      ctrl.index.abort();
      ctrl.walk.abort();
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
    // A query change is deliberately NOT a revalidation boundary any more.
    //
    // It used to be, on the reasoning that the rows are being replaced anyway
    // so a deferred generation lands for free. It is not free: adopting the
    // generation invalidates the corpus, which re-runs the fetch, which means
    // every keystroke that arrives after any background churn pays a round
    // trip before it can rank anything. Ranking the new query against the
    // corpus already in hand — a generation old, dimmed, and captioned — is
    // both instant and honest, and being a generation behind is a state this
    // search can simply live in (listing/revalidate).
    //
    // Editing the query is still a user gesture, so it is still the retry for
    // a failed walk: otherwise search stays dead until something else moves.
    // (An idle walk needs no handling here — the auto-request effect fires
    // as soon as the non-empty query state lands.)
    if (validWalk.status === "error") {
      setWalkReq(pinned);
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

  // The corpus to rank. Background churn no longer invalidates it at all — a
  // keystroke ranks against whatever is in hand (see setQuery) — so the hold
  // covers what is left: the search ending and being reopened, and a fetch
  // that is genuinely re-running. The previous generation's entries stay
  // scannable so those never blank the rows either (listing/corpus-hold). The
  // hold is captured from `validWalk` during render, exactly like the
  // ranked-hit hold below — by the render where `pinned` moves and validWalk
  // reads idle, this ref is already carrying the corpus from the render before
  // it, unless the reconcile above deliberately dropped it.
  heldCorpus.current = nextHeldCorpus(validWalk, heldCorpus.current);
  const corpus = scannableCorpus(searching, validWalk, heldCorpus.current);

  // The scan itself — incremental, sliced and cancellable, shared with the
  // explorer home page's box (listing/useRankedScan carries the reasoning).
  const { ranked, pending } = useRankedScan(corpus.entries, q, SCAN_DEBOUNCE_MS, corpus.key);

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
  // `corpus.stale` — rows from the hold while a fetch is actually running —
  // joins the two pre-existing reasons: rows ranked against a previous
  // generation's corpus are exactly as provisional as rows the scan has not
  // finished producing, and all of them must read as such. All three are
  // MOMENTARY, which is what the heavy dim they drive is calibrated for.
  const isStale = deferredStale || scanPending || corpus.stale;
  // Being a generation behind is not momentary: the folder or the index moved
  // and this search deliberately did not follow, and it will stay that way
  // until a real boundary (listing/revalidate). That is a fine state to be in,
  // but only if it is visible — "stale but honestly labelled" is the whole
  // deal. It gets a treatment of its own rather than the in-flight one, because
  // a dim that can persist for the whole session has to be legible to read
  // under, and because "an answer is coming" and "no answer is coming, this is
  // it" are different claims. Reported separately for exactly that reason.
  const generationBehind = searching && pinned !== gen;

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
    // "These results are computed from an older generation of the tree, and
    // nothing is on its way to fix that" — see above. Drives the caveat chip
    // and its own, lighter dim.
    behind: generationBehind,
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
