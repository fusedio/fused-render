// The in-folder search: query state (URL-synced), the ranked answer from the
// index, the live streamed walk for the folders the index cannot cover, the
// stale-while-revalidate holds, and the result cap.
//
// A non-empty query swaps the listing for flat, rank-ordered results over the
// whole subtree. WHERE those results come from is the server's call, never
// this file's: `GET /api/index/rank` filters and ranks in the index and says
// WHY when it cannot (listing/index-source), and only a folder no scan will
// ever cover — a remote mount, a package, a folder the ignore list excludes —
// falls through to the live walk.
//
// This used to be a RACE. The box asked the index for the folder's entire
// corpus and, if that had not produced within 150 ms, started the walk
// alongside it; first to produce took the whole answer. Two things ended that:
// the corpus is a payload the size of the folder (19.8 MB on a big home) where
// a ranked answer is a few KB, and the reason the index cannot answer is a
// fact only the server holds — so racing a second source against it was
// guessing at a question that has an answer.
//
// Both sources feed the same downstream, and the two behave differently in one
// respect that is deliberate rather than incidental:
//
//   * the WALK ranks in the browser (listing/useRankedScan) over a corpus that
//     arrives in batches, so a keystroke re-ranks what is already in hand,
//     within a frame, and the rows for a new query never wait on a network. It
//     keeps the corpus hold and the query-tagged result hold it always had.
//   * the INDEX answers per query, so a keystroke costs a round trip. Every
//     rule phase 1 established for the home page's box applies here for the
//     same reason (platform/lib/instant-search): fire on the leading edge,
//     coalesce a burst, abort rather than queue, answer a backspace from
//     memory — and NEVER blank the list. The previous query's rows stay on
//     screen, dimmed and captioned, until the next answer lands. That is the
//     one place this box deliberately departs from lib/search-hold's rule of
//     never rendering rows under a query they were not computed for: with a
//     local corpus that rule cost nothing, and with a round trip it is the
//     difference between "instant" and a list that flashes empty per keystroke.
//
// Neither source re-fetches on background churn. A dir-watch event or a scan
// completing is RECORDED, and the results stay put — dimmed and captioned "not
// refreshed" — until a boundary where a repaint costs the user nothing: the
// search ending, or a change this app itself made. See listing/revalidate.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { indexRank, requestFolderScan, walkDirStream } from "@platform/lib/api";
import type { IndexRankResult, WalkEntry } from "@platform/lib/api";
import {
  corpusKey,
  nextHeldCorpus,
  scannableCorpus,
  type HeldCorpus,
} from "@apps/explorer/listing/corpus-hold";
import {
  fsMutationCount,
  indexLifecycleCount,
  indexRescanPending,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { replaceSearch } from "@platform/lib/router";
import { PENDING_INDICATOR_MS, QueryMemo, searchDelay } from "@platform/lib/instant-search";
import { nextHeldHits, resolveDisplayedHits, type QueryTagged } from "@platform/lib/search-hold";
import { useRankedScan } from "@apps/explorer/listing/useRankedScan";
import { shouldReconcile } from "@apps/explorer/listing/revalidate";
import { capHits } from "@apps/explorer/listing/result-cap";
import { hitsFromRank } from "@apps/explorer/listing/ranked-hits";
import {
  nextStep,
  remembersAnswer,
  searchProgress,
} from "@apps/explorer/listing/index-source";
import {
  IDLE_WALK,
  RERANK_COMMIT_MS,
  SCAN_DEBOUNCE_MS,
  SEARCH_RANK_LIMIT,
  SCAN_POLL_MS,
  STREAM_FLUSH_MS,
  URL_SYNC_MS,
  type SearchHit,
  type WalkState,
} from "@apps/explorer/listing/types";

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
}

// One answered query: the rows, and what they are an answer TO. Carrying the
// query is what lets the box never blank — see the header.
interface RankAnswer {
  query: string;
  hits: SearchHit[];
  truncated: boolean;
  total: number;
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
  const [walkReq, setWalkReq] = useState<number | null>(null);
  // Bumped to re-run the request after an error, from a real user gesture only
  // (focus / typing) — an effect-driven retry would loop forever.
  const [retryNonce, setRetryNonce] = useState(0);

  // The input echoes `query` (immediate) so keystrokes never wait on the
  // fuzzy-scoring/rendering work below. `deferredQuery` trails behind under
  // load — React commits a cheap render with the old deferred value first
  // (echoing the keystroke), then a low-priority render picks up the new
  // value and redoes the expensive work, interruptible by further typing.
  const deferredQuery = useDeferredValue(query);
  const q = deferredQuery.trim();
  const searching = q !== "";
  // `isStale` is completed below, once the request's own pending state is
  // known: the input can have settled while the answer for it is in flight.
  const deferredStale = query.trim() !== q;

  // The index being deleted or a scan completing dates the answer the same way
  // a dir-watch bump does, and needs its own signal: the filesystem didn't
  // change, so no watch refresh will ever re-key anything
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
    // is the one thing search must never offer back. Every other reconcile is
    // background churn, where holding is the point.
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

  // --- which source answers this folder --------------------------------------
  //
  // The index, until IT says otherwise. A folder no scan can cover (a mount, a
  // package, one the ignore list excludes) and a folder that stayed uncovered
  // after we asked for it to be scanned both come back as `walk` from
  // listing/index-source, and that is the only way this flag is ever set: the
  // client holds no copy of the rules behind the verdict.
  const [walkMode, setWalkMode] = useState(false);
  // A scan has been asked for, for this folder and generation, and how many
  // answers have arrived since. Both reset with the folder or the generation.
  const asked = useRef(false);
  const sinceAsk = useRef(0);
  const polls = useRef(0);
  // Bumped by the poll timer to re-ask while a scan is running.
  const [pollTick, setPollTick] = useState(0);
  const [polling, setPolling] = useState(false);
  // Whether the LAST answer covered the folder, for the decision at the poll
  // ceiling: settling for what we have is only honest if there is something.
  const covered = useRef(false);
  // Which folder+generation the async replies below still speak for.
  //
  // Two requests outlive the effect that issued them — the scan ask and the
  // focus probe — and both end in `setWalkMode(true)`. Neither is abortable in
  // any useful sense (aborting the fetch would not stop a scan the server has
  // already started), so they are tagged instead: a reply from before a
  // navigation or a reconcile is dropped rather than applied to whatever the
  // box is showing now. Without this, a `refused` for the folder you just left
  // pins the folder you just opened to the live walk — and since only a
  // generation change clears `walkMode`, it stays pinned. This hook is NOT
  // remounted per folder (only the embedded listing is keyed on its path), so
  // there is no mount boundary doing this for us.
  const sourceEpoch = useRef(0);
  useEffect(() => {
    sourceEpoch.current += 1;
    setWalkMode(false);
    asked.current = false;
    sinceAsk.current = 0;
    polls.current = 0;
    covered.current = false;
    setPolling(false);
  }, [fsPath, pinned]);

  // --- the ranked answer ------------------------------------------------------
  const [answer, setAnswer] = useState<RankAnswer | null>(null);
  const [failure, setFailure] = useState("");
  const [pending, setPending] = useState(false);
  const memo = useRef(new QueryMemo<RankAnswer>());
  const inflight = useRef<AbortController | null>(null);
  // Identity of the request in flight (folder, generation, attempt, query), or
  // null. See the guard in the fetch effect.
  const inflightKey = useRef<string | null>(null);
  const issuedAt = useRef(0);
  const answerSeq = useRef(0);
  useEffect(() => {
    memo.current.clear();
    setAnswer(null);
    setFailure("");
  }, [fsPath, pinned]);
  useEffect(() => () => inflight.current?.abort(), []);

  // What to do with an answer: render it, ask for a scan, poll, or hand the
  // folder to the live walk. The decision itself is pure (listing/index-source);
  // this is the wiring for it.
  const applyStep = (res: IndexRankResult) => {
    if (asked.current) sinceAsk.current += 1;
    covered.current = res.covered;
    const step = nextStep({
      reason: res.reason ?? "",
      asked: asked.current,
      sinceAsk: sinceAsk.current,
      polls: polls.current,
      covered: res.covered,
    });
    if (step === "walk") {
      setWalkMode(true);
      setPolling(false);
      return step;
    }
    if (step === "scan") {
      asked.current = true;
      sinceAsk.current = 0;
      setPolling(true);
      // A refusal is durable — mount-backed, gone, or scanned too recently to
      // scan again (server/routers/index.py). Nothing is coming, so stop
      // polling for it and let the walk answer, which is what the folder would
      // have got before any of this existed. Tagged with the epoch, because
      // this reply can land on a folder the box has since navigated away from.
      const epoch = sourceEpoch.current;
      void requestFolderScan(fsPath).then(
        (r) => {
          if (sourceEpoch.current !== epoch) return;
          if (!r.started) {
            setPolling(false);
            setWalkMode(true);
          }
        },
        () => {
          if (sourceEpoch.current !== epoch) return;
          setPolling(false);
          setWalkMode(true);
        },
      );
      return step;
    }
    if (step === "poll") {
      setPolling(true);
      return step;
    }
    polls.current = 0;
    setPolling(false);
    return step;
  };

  // ONE REQUEST PER QUERY, abortable, and never queued: the answer to a query
  // the user has already edited is worth nothing, and letting it land would
  // repaint the list backwards.
  useEffect(() => {
    if (walkMode || !searching) {
      inflight.current?.abort();
      inflightKey.current = null;
      setPending(false);
      // Leaving search drops the answer. Keeping it would carry one search
      // session's rows into the next one's first frame under a query they
      // have nothing to do with — the never-blank rule is about a query being
      // EDITED, not about search being closed and reopened. The memo keeps
      // the round trip cheap if the same query comes back.
      if (!searching) setAnswer(null);
      return;
    }
    // While a scan is running the remembered answer is exactly the one that is
    // out of date, so the memo is consulted only when nothing is on its way.
    const remembered = polling ? undefined : memo.current.get(q);
    if (remembered) {
      inflight.current?.abort();
      inflightKey.current = null;
      setAnswer(remembered);
      setFailure("");
      setPending(false);
      return;
    }
    // The request this run would issue. Compared against the one already out,
    // so a poll tick does not abort a live request and start it again: a rank
    // that outlasts SCAN_POLL_MS would otherwise never be allowed to finish,
    // and the loop would outlive the scan it is waiting for. It also drops the
    // duplicate that the `polling` flag flipping used to cost.
    const key = [fsPath, pinned, retryNonce, q].join("\u0000");
    if (inflightKey.current === key) return;
    const run = () => {
      inflight.current?.abort();
      inflightKey.current = key;
      const ctl = new AbortController();
      inflight.current = ctl;
      issuedAt.current = Date.now();
      setPending(true);
      // The previous failure is not this request's verdict.
      setFailure("");
      indexRank(fsPath, q, { signal: ctl.signal, limit: SEARCH_RANK_LIMIT }).then(
        (res) => {
          if (ctl.signal.aborted) return;
          inflightKey.current = null;
          const step = applyStep(res);
          answerSeq.current += 1;
          const next: RankAnswer = {
            query: q,
            hits: hitsFromRank(res.hits, q),
            truncated: res.truncated,
            total: res.total,
          };
          // Remembered only once nothing is on its way to change it: an answer
          // taken mid-scan is a snapshot of a folder still being indexed. The
          // decision reads the step we just computed, never the `polling`
          // state — that state is one commit behind here, so the answer that
          // ENDS a scan would be the one answer never remembered.
          if (remembersAnswer(step, res.reason ?? "")) memo.current.put(q, next);
          setAnswer(next);
          setFailure("");
          setPending(false);
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          inflightKey.current = null;
          // The rows in hand STAY (see the header); the error only reaches the
          // screen when there is nothing else to show.
          setFailure(err.message);
          setPending(false);
        },
      );
    };
    // Zero on the leading edge — the first keystroke after a pause must not
    // sit behind a timer (lib/instant-search, `searchDelay`).
    const delay = searchDelay(Date.now(), issuedAt.current);
    if (delay === 0) {
      run();
      return;
    }
    const timer = window.setTimeout(run, delay);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- applyStep is
    // recreated each render; everything it reads is a ref or listed here.
  }, [fsPath, q, searching, walkMode, pinned, retryNonce, pollTick, polling]);

  // The poll itself: while a scan covering this folder is running, ask again
  // on a modest cadence and repaint. The ordering WILL shift as rows land;
  // what must not happen is the list going empty between repaints, and it
  // cannot — the previous answer stays until the next one replaces it.
  //
  // The TICK is what is counted against the ceiling, not the answers. A poll
  // whose request keeps outlasting the interval produces no answers at all, so
  // a ceiling counted in answers is one this loop can starve — and the loop
  // would then outlive the scan it exists to wait for. Counting here bounds it
  // in wall-clock time whatever the server does.
  useEffect(() => {
    if (!polling || !searching || walkMode) return;
    const timer = window.setTimeout(() => {
      polls.current += 1;
      const step = nextStep({
        reason: "scanning",
        asked: asked.current,
        sinceAsk: sinceAsk.current,
        polls: polls.current,
        covered: covered.current,
      });
      if (step === "poll") {
        setPollTick((n) => n + 1);
        return;
      }
      // Out of patience — the same rule the answer path uses, so there is one
      // definition of what running out means (listing/index-source).
      setPolling(false);
      if (step === "walk") setWalkMode(true);
    }, SCAN_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [polling, searching, walkMode, pollTick]);

  // --- the live walk, for the folders the index cannot cover ------------------
  //
  // Unchanged in what it does: a streamed NDJSON walk whose batches push into
  // one append-only array, aborted on cleanup (which closes the server-side
  // generator). It runs only in `walkMode`, and only while a search is active.
  useEffect(() => {
    if (!walkMode) return;
    if (query.trim() !== "" && validWalk.status === "idle" && walkReq !== pinned) {
      setWalkReq(pinned);
    }
  }, [walkMode, query, validWalk.status, walkReq, pinned]);

  useEffect(() => {
    if (!walkMode || walkReq === null) return;
    const forRefresh = walkReq;
    const ctrl = new AbortController();
    let alive = true;
    const entries: WalkEntry[] = [];
    // Flush throttle (STREAM_FLUSH_MS): entries accumulate in `pending`
    // between commits so the scoring/render work runs a few times a second,
    // not once per network chunk. A trailing timer guarantees the last
    // partial interval still commits.
    let batched: WalkEntry[] = [];
    let lastFlush = 0;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    // `retryNonce` is in the corpus identity because a retry is a fresh walk
    // of the filesystem under an unchanged folder and generation — see
    // corpusKey.
    const walkKey = corpusKey("walk", fsPath, forRefresh, retryNonce);
    const flush = () => {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      for (const e of batched) entries.push(e); // no spread: a big chunk would blow the arg limit
      batched = [];
      lastFlush = Date.now();
      setWalk({ status: "streaming", entries, count: entries.length, key: walkKey, forRefresh });
    };
    // Published BEFORE the walk has answered, so search reads as "in flight"
    // from this instant. It is deliberately empty rather than preserving the
    // old rows: what stands in meanwhile is the HELD corpus
    // (listing/corpus-hold), which keeps that stand-in explicitly marked stale
    // instead of letting an unsettled state quietly carry last generation's
    // rows under a fresh tag.
    setWalk({ status: "streaming", entries, count: 0, key: walkKey, forRefresh });
    void walkDirStream(fsPath, {
      hidden: true,
      signal: ctrl.signal,
      onBatch: (batch) => {
        if (!alive) return;
        for (const e of batch) batched.push(e);
        const wait = STREAM_FLUSH_MS - (Date.now() - lastFlush);
        if (wait <= 0) flush();
        else if (flushTimer === null) flushTimer = setTimeout(() => alive && flush(), wait);
      },
    }).then(
      (end) => {
        if (!alive) return;
        if (flushTimer !== null) clearTimeout(flushTimer);
        for (const e of batched) entries.push(e);
        batched = [];
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
      },
    );
    return () => {
      alive = false;
      if (flushTimer !== null) clearTimeout(flushTimer);
      ctrl.abort();
    };
  }, [walkMode, fsPath, walkReq, retryNonce]);

  // First focus warms the answer in the background; focus (like typing below)
  // is also the retry gesture after a failure.
  //
  // Focus is deliberately NOT a revalidation boundary. It reads like one — the
  // user is "coming back to" the search — but it is ambient: the pane focus
  // guard, a split remount at a width threshold, and WebKit restoring focus
  // after a repaint all fire it with no gesture behind them. Treating it as a
  // boundary adopted whatever churn had accumulated and swapped the results
  // out from under someone who was reading them, which is the exact thing the
  // deferral exists to prevent. Requests are tagged `pinned`, never `refresh`,
  // so warming here cannot smuggle a newer generation in either.
  const probed = useRef("");
  const prefetchWalk = () => {
    if (walkMode) {
      if (validWalk.status === "idle") setWalkReq(pinned);
      else if (validWalk.status === "error") {
        setWalkReq(pinned);
        setRetryNonce((n) => n + 1);
      }
      return;
    }
    if (failure !== "") setRetryNonce((n) => n + 1);
    // One cheap probe per folder+generation. It pays the server's cold cost
    // (the duckdb import, the gitignore pool) before the first keystroke, and
    // it learns the SOURCE — so a mount-backed folder has its walk running by
    // the time a character is typed, exactly as when focus started the walk
    // directly. It never asks for a scan: focus is not a request for one.
    const probeKey = fsPath + ":" + pinned;
    if (probed.current === probeKey) return;
    probed.current = probeKey;
    const epoch = sourceEpoch.current;
    void indexRank(fsPath, "", { limit: 1 }).then(
      (res) => {
        // Same staleness as the scan ask: a probe issued for the folder you
        // just left must not pin the one you just opened to the walk.
        if (sourceEpoch.current !== epoch) return;
        // Through the same decision as a real answer, so there is one place
        // that knows which verdicts mean "walk". `asked: false` keeps a probe
        // from ever counting as the on-demand scan's first look.
        const step = nextStep({ reason: res.reason ?? "", asked: false,
                                sinceAsk: 0, polls: 0,
                                covered: res.covered });
        if (step === "walk") setWalkMode(true);
      },
      () => {},
    );
  };

  // Debounced URL mirror for the query (see URL_SYNC_MS). Pending sync is
  // dropped on unmount — a navigation has already replaced the URL by then.
  const urlTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(
    () => () => {
      if (urlTimer.current !== null) clearTimeout(urlTimer.current);
    },
    [],
  );

  const setQuery = (value: string) => {
    setQueryState(value);
    // A query change is deliberately NOT a revalidation boundary.
    //
    // It used to be, on the reasoning that the rows are being replaced anyway
    // so a deferred generation lands for free. It is not free: adopting the
    // generation invalidates everything in hand, which re-runs the fetch,
    // which means every keystroke that arrives after any background churn
    // pays for a generation nobody asked to move to. Being a generation behind
    // is a state this search can simply live in (listing/revalidate).
    //
    // Editing the query is still a user gesture, so it is still the retry for
    // a failed request: otherwise search stays dead until something else moves.
    if (validWalk.status === "error" || failure !== "") {
      if (walkMode) setWalkReq(pinned);
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

  // --- the walk's browser-side ranking ----------------------------------------
  // The corpus to rank, and the hold that keeps a refetch from blanking the
  // rows (listing/corpus-hold). Captured from `validWalk` during render,
  // exactly like the ranked-hit hold below.
  heldCorpus.current = nextHeldCorpus(validWalk, heldCorpus.current);
  const corpus = scannableCorpus(searching && walkMode, validWalk, heldCorpus.current);

  // The scan itself — incremental, sliced and cancellable (listing/useRankedScan
  // carries the reasoning). Only a walk feeds it: the index answers ranked.
  const { ranked, pending: scanning } = useRankedScan(
    corpus.entries, q, SCAN_DEBOUNCE_MS, corpus.key);

  // Relevance (the fuzzy rank) is the only order search results have. Column
  // sorting used to be offered here and was withdrawn with the Size/Modified
  // headers: the hit set is capped and, mid-walk, partial — a by-date or
  // by-size ordering over it reads as an answer to a question the data cannot
  // answer, and the search UI already warns the coverage is approximate.
  const walkHits = useMemo(
    () => (searching && walkMode ? ranked : []), [searching, walkMode, ranked]);

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
    items: walkHits,
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
      setRankedForRender({ q, items: walkHits });
      return;
    }
    const wait = RERANK_COMMIT_MS - (Date.now() - lastRankCommit.current);
    const commit = () => {
      lastRankCommit.current = Date.now();
      setRankedForRender({ q, items: walkHits });
    };
    if (wait <= 0) {
      commit(); // includes the first flush of a stream — first paint isn't delayed
      return;
    }
    const id = window.setTimeout(commit, wait);
    return () => window.clearTimeout(id);
  }, [walkHits, q, validWalk.status]);

  // --- Stale-while-revalidate for the walk's results (B3) ---------------------
  // A dir-watch event makes validWalk read idle for the stale generation, which
  // collapses the hits to [] — so the entire visible result list used to blank
  // to "Searching…" while the tree re-walked, for as long as that takes on a
  // big folder. Hold the last ranked answer and keep rendering it (dimmed, with
  // the spinner) until the fresh one is ready.
  //
  // This changes only what is DISPLAYED: these held rows are never fed back
  // into scoring, so a ranking is always something the scorer actually
  // produced rather than something reassembled here. Both halves of the
  // decision — what to retain, and what to render — live in lib/search-hold,
  // pure and query-tagged. The hold applies only while the current-generation
  // walk is unsettled: a COMPLETED walk with no hits is a real "no matches"
  // answer (the file was just deleted, say) and replaces the held rows.
  const heldHits = useRef<QueryTagged<SearchHit> | null>(null);
  heldHits.current = nextHeldHits(searching && walkMode, q, rankedForRender, heldHits.current);
  const walkUnsettled = validWalk.status === "idle" || validWalk.status === "streaming";
  const walkDisplay = resolveDisplayedHits(
    searching && walkMode,
    q,
    rankedForRender,
    heldHits.current,
    walkUnsettled,
  );

  // --- what the two sources agree to hand over --------------------------------
  //
  // The ranked answer is rendered whatever query it answers (see the header),
  // so `staleRows` is how the caller learns to dim it. The walk's own hold is
  // query-tagged and reports the same fact as `showingHeld`.
  const indexRows = searching && !walkMode ? (answer?.hits ?? []) : [];
  const staleRows = !walkMode && answer !== null && answer.query !== q;
  const hits = walkMode ? walkHits : indexRows;
  const displayHits = walkMode ? walkDisplay.hits : indexRows;
  // The walk's hold is query-tagged, so "these rows are held" is a distinct
  // claim there. The index has no equivalent: rows for a query the user has
  // moved past are reported through `behind` (the lighter, persistent dim),
  // and while an answer is in flight they are `isStale` — saying it twice
  // would stack two dims calibrated for different things.
  const showingHeld = walkMode ? walkDisplay.showingHeld : false;

  // Whether the rendered rows are an ANSWER to the query in the box.
  //
  // Outside search, and on the walk path, this is structurally true: the walk
  // ranks a corpus already in hand, so a keystroke re-ranks within a frame,
  // and lib/search-hold refuses to render rows tagged with another query. The
  // ranked path is the one that can show rows for a query the user has typed
  // past, because it deliberately never blanks the list while the next answer
  // is in flight — so it has to SAY so, and the sayer is this flag.
  //
  // It gates the guesses the listing makes on the user's behalf: auto-selecting
  // the top hit (listing/selection) and Enter opening row 0 with nothing
  // selected (useListingSelection). Both would otherwise act on a file that
  // answers nothing the user typed — and the selection they leave behind is
  // what Cmd+Backspace acts on too.
  const rowsAnswerQuery = !searching || walkMode || (!staleRows && !pending);

  // The rendered rows: the top of the ranking only (listing/result-cap). This
  // is also what keyboard nav and auto-select walk, so they never address a
  // row that is not on screen.
  const visibleHits = useMemo(() => capHits(displayHits), [displayHits]);

  // How many ranked matches the cap is hiding — the counter reports the true
  // total and tells the user to narrow the query (listing/result-cap). There
  // is deliberately no "load more": scrolling further is the wrong answer.
  const cappedAway = displayHits.length - visibleHits.length;

  // The search's state in the shape the walk always reported it in, so the
  // rendering does not have to know which source answered. For the index that
  // is: in flight with nothing to show yet is `streaming` (count 0 — there is
  // no entries-scanned progress to claim), a failure with nothing to show is
  // `error`, and anything else is a settled `ok` carrying the truncation the
  // count chip owns up to.
  const indexState: WalkState = !searching
    ? IDLE_WALK
    : failure !== "" && displayHits.length === 0
      ? { status: "error", message: failure, key: "rank", forRefresh: pinned }
      : answer === null
        ? { status: "streaming", entries: [], count: 0,
            key: corpusKey("index", fsPath, pinned, answerSeq.current), forRefresh: pinned }
        : { status: "ok", entries: [], truncated: answer.truncated, total: answer.total,
            key: corpusKey("index", fsPath, pinned, answerSeq.current), forRefresh: pinned };
  const searchState = walkMode ? validWalk : indexState;

  // True while an answer for the current query has not arrived yet — the
  // "Searching…" row keys off this, so it has to mean "an answer is still
  // coming", not merely "the deferred value lags".
  // Two questions, not one (listing/index-source): whether an answer is still
  // coming, and whether the wait is the momentary kind. A scan landing rows is
  // the first without being the second.
  const progress = searchProgress({ searching, walkMode, pending, polling, scanning });
  const scanPending = progress.answerComing;
  // Momentary states: a request in flight, a deferred value that has not
  // caught up, or rows ranked against a corpus a generation old. All three get
  // the heavy dim, which is calibrated for something that clears in a moment —
  // so a scan running for a minute is deliberately NOT one of them; it has the
  // "indexing…" caveat instead.
  const isStale = deferredStale || progress.inFlight || (walkMode && corpus.stale);
  // Being a generation behind is NOT momentary: the folder or the index moved
  // and this search deliberately did not follow, and it will stay that way
  // until a real boundary (listing/revalidate). Rows answering a query the
  // user has already edited past, with nothing in flight to replace them, are
  // the same kind of claim — "no answer is coming, this is it" — so they join
  // it rather than the dim above.
  const generationBehind = searching && (pinned !== gen || (staleRows && !pending));

  // No spinner flash: a pending indicator appears only once being pending is
  // information rather than a flicker. The common answer lands well inside
  // PENDING_INDICATOR_MS.
  const unsettled = searching && (scanPending || searchState.status === "streaming");
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!unsettled) {
      setSlow(false);
      return;
    }
    const timer = window.setTimeout(() => setSlow(true), PENDING_INDICATOR_MS);
    return () => window.clearTimeout(timer);
  }, [unsettled]);

  return {
    query,
    setQuery,
    q,
    searching,
    isStale,
    // "These results are computed from an older generation of the tree, or for
    // a query that has moved on, and nothing is on its way to fix that" — see
    // above. Drives the caveat chip and its own, lighter dim.
    behind: generationBehind,
    scanPending,
    /** Whether an answer is still coming AND has taken long enough to say so. */
    spinner: unsettled && slow,
    // This app changed a file and the rescan it triggered has not landed yet
    // (server/index_touch.py). Read during render, and re-read whenever it can
    // have moved: both signals that change it are subscribed above.
    rescanPending: indexRescanPending(),
    validWalk: searchState,
    /** Which source answered. The two report progress in different units. */
    source: walkMode ? ("walk" as const) : ("index" as const),
    prefetchWalk,
    hits,
    displayHits,
    visibleHits,
    showingHeld,
    rowsAnswerQuery,
    cappedAway,
  };
}
