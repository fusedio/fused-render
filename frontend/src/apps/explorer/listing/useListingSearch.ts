// The in-folder search: query state (URL-synced), and the ranked answer from
// the index.
//
// A non-empty query (at least MIN_QUERY_CHARS long — the same gate the home
// page's box uses, lib/home-search) swaps the listing for flat, rank-ordered
// results over the whole subtree — UNLESS the query is path-shaped and
// glob-free (`isPathQuery`, path-shaped-query.ts), in which case no request
// is ever issued at all: the completion dropdown already answers that query
// on its own (`useCompletion.ts`), and a rank request behind the same text
// would only echo it back with a count nobody asked for. WHERE those results
// come from, when they do run, is the
// server's call, never this file's: `GET /api/index/rank` filters and ranks
// in the index and says WHY when it cannot yet (listing/index-source). A
// folder no scan will ever cover — a remote mount, a package, one the ignore
// list excludes — comes back with a `reason` this hook hands to the caller
// unchanged; the caller renders it as the same index gap the home page's box
// already shows for the same reasons (lib/home-search's `indexGap`).
//
// An uncovered folder — a remote mount, a package, one the ignore list
// excludes — asks for an on-demand scan and polls while it runs
// (listing/index-source), and a folder that stays uncovered, or that no scan
// will ever cover in the first place, settles for the index's own answer
// about itself: there is no second, browser-scored path that walks the
// filesystem directly. Every rule phase 1 established for the home page's box
// applies here for the same reason it always did (platform/lib/instant-
// search): a trailing debounce, abort rather than queue, answer a backspace
// from memory — and NEVER blank the list. The previous query's rows stay on
// screen, dimmed and captioned, until the next answer lands.
//
// Neither the ranked answer nor the scan poll re-fetches on background churn.
// A dir-watch event or a scan completing elsewhere is RECORDED, and the
// results stay put — dimmed and captioned "not refreshed" — until a boundary
// where a repaint costs the user nothing: the search ending, or a change this
// app itself made. See listing/revalidate.
import { useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { indexRank, requestFolderScan } from "@platform/lib/api";
import type { IndexRankResult, RankReason } from "@platform/lib/api";
import {
  fsMutationCount,
  indexLifecycleCount,
  indexRescanPending,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { escapesFsPath } from "@apps/explorer/listing/query-base";
import { isPathShapedQuery } from "@apps/explorer/listing/path-shaped-query";
import { navHintQCommitted, replaceSearch } from "@platform/lib/router";
import { INSTANT_DEBOUNCE_MS, PENDING_INDICATOR_MS, QueryMemo } from "@platform/lib/instant-search";
import { MIN_QUERY_CHARS } from "@apps/explorer/lib/home-search";
import { useRankedSearchEnabled } from "@apps/explorer/lib/ranked-search-pref";
import { shouldReconcile } from "@apps/explorer/listing/revalidate";
import { capHits } from "@apps/explorer/listing/result-cap";
import { hitsFromRank } from "@apps/explorer/listing/ranked-hits";
import { nextStep, remembersAnswer, searchProgress } from "@apps/explorer/listing/index-source";
import {
  IDLE_SEARCH,
  SCAN_POLL_MS,
  SEARCH_GLOB_RANK_LIMIT,
  SEARCH_RANK_LIMIT,
  URL_SYNC_MS,
  type SearchHit,
  type SearchState,
} from "@apps/explorer/listing/types";

function currentQuery(): string {
  return new URLSearchParams(location.search).get("q") || "";
}

// One answered query: the rows, and what they are an answer TO. Carrying the
// query is what lets the box never blank — see the header.
interface RankAnswer {
  query: string;
  /**
   * The generation this answer was FETCHED under.
   *
   * It travels with the rows because a remembered answer is a snapshot: served
   * back from the memo two watch bumps later, it is exactly as stale as the
   * moment it was taken, and reading the caption off a counter that has since
   * moved said "current" over rows that were not.
   */
  gen: number;
  hits: SearchHit[];
  truncated: boolean;
  total: number;
  reason: RankReason;
  // The directory `hits` are relative to — `res.base` from the server, not
  // necessarily `fsPath`: a `~`/`/`-leading query can walk `resolve_query`
  // (fused_render/index/query.py) out past the folder being searched. A
  // caller building an absolute row path from `entry.rel` has to join it
  // onto THIS, not onto `fsPath` (api.ts's `IndexRankResult.base` doc).
  base: string;
  // Which mode the server actually ran (resolve_query's own call — see
  // SPEC-one-search-language.md), never re-derived client-side: it is what
  // decides which cap applies below (listing/result-cap).
  mode: "substring" | "glob";
  // Decision 10: `Date.now()` at issue to `Date.now()` when this answer was
  // applied — end-to-end latency the user actually felt, matching
  // FilesHome.tsx's home search (home-search.ts's `elapsedMs` doc). Baked
  // into the answer at fetch time and never recomputed: a memoized reply
  // (the `memo.current.get(q)` hit below) hands this same object back
  // verbatim, so a cache hit reports the real cost of the request that
  // actually ran, not ~0ms for a reply that just came from memory.
  elapsedMs: number;
}

// `urlSync=false` (an embedded Listing, e.g. the preview pane's `_listing`
// mode) keeps the query fully local: it neither seeds from ?q nor mirrors
// keystrokes back to the address bar — that URL belongs to the host view.
//
// `home` is here for exactly one thing: resolving `isPathQuery` below (a
// leading "~" needs it the same way `listingAddress` always has). Threaded in
// rather than read some other way so this stays the one place that answer is
// computed, for the one hook that both drives the rank request AND is the
// query's source of truth.
export function useListingSearch(
  fsPath: string,
  home: string | undefined,
  refresh: number,
  urlSync = true,
) {
  // The owner's unranked-search preference (D720) — same module-level cache
  // FilesHome.tsx's home search reads; both boxes honour the one setting.
  const rankedPref = useRankedSearchEnabled();
  const [query, setQueryState] = useState<string>(() => (urlSync ? currentQuery() : ""));
  // Bumped to re-run the request after an error, from a real user gesture only
  // (focus / typing) — an effect-driven retry would loop forever.
  const [retryNonce, setRetryNonce] = useState(0);

  // The input echoes `query` (immediate) so keystrokes never wait on the
  // rendering work below. `deferredQuery` trails behind under load — React
  // commits a cheap render with the old deferred value first (echoing the
  // keystroke), then a low-priority render picks up the new value.
  const deferredQuery = useDeferredValue(query);
  const q = deferredQuery.trim();
  // Below MIN_QUERY_CHARS the query is too short to be worth a request — the
  // same gate the home page's box uses, and for the same reason: a
  // single-character rank request is mostly noise.
  const searching = q.length >= MIN_QUERY_CHARS;
  // `isStale` is completed below, once the request's own pending state is
  // known: the input can have settled while the answer for it is in flight.
  const deferredStale = query.trim() !== q;

  // Decision 5 revisited: a path-shaped, non-glob query (`path-shaped-
  // query.ts`) never runs a rank request — the same "any search on an
  // unpatterned path is useless" call the chip's own word choice makes
  // (SearchField.tsx's `chipIsSearch`), computed once here so the two can
  // never disagree. Read off the LIVE `query`, not the deferred `q` below:
  // the chip flips on every keystroke, not a beat behind it, and this is the
  // one value both this hook's own gate and every caller (Listing.tsx,
  // FileSearchField.tsx) read for that same immediate answer.
  const isPathQuery = isPathShapedQuery(query, fsPath, home);
  // What `searching` meant before this predicate existed: a real search is
  // actually going to run. `searching` itself stays pure length — the box
  // still visually expands for a typed path the same as for a typed filter —
  // so every place below that means "is a rank request live or landing" reads
  // this instead, never the raw flag.
  const runsSearch = searching && !isPathQuery;

  // A query whose base genuinely differs from the folder being searched
  // waits for an explicit commit (Enter, via `commitSearch`) rather than
  // live-filtering: that base can walk arbitrarily far from the open
  // folder, which is the "thousands of folders searched per keystroke" case
  // a half-typed one would otherwise produce. A glob anchored at the box
  // root, like "*/*.json", never leaves the folder being searched and
  // live-filters like plain text — and NEITHER does an absolute/tilde query
  // that resolves right back inside `fsPath` (SPEC-omnibox-search-
  // affordance.md correction, 2026-09-10): the box always arrives pre-filled
  // with `fsPath`'s own absolute path, so appending a pattern to what's
  // already there is the single most natural gesture here, and it should
  // live-filter exactly like the equivalent relative query, not wait for
  // Enter. `escapesFsPath` (query-base.ts) is the fsPath-aware predicate
  // this needs — `escapesBase` alone can't tell a same-subtree absolute
  // path from a genuinely different one with no `fsPath` to compare against,
  // and it stays as it is for `isPathQuery` (path-shaped-query.ts), which
  // asks a different question and would regress if it changed meaning.
  const escapes = escapesFsPath(q, fsPath, home);
  // The specific query text Enter was last pressed for. A ref, not state: it
  // must not itself cause a render, only unlock the fetch effect below (which
  // re-runs on `gateNonce`).
  //
  // Committed against `query` (live), never `q` (deferred): under load the
  // deferred value can still be trailing the keystroke Enter was pressed
  // right after, and a commit recorded against that stale trailing value
  // would stop matching once the deferred render catches up a moment later
  // — reopening the gate would then need a second Enter. Recording the live
  // text instead means `committedGate.current === q` starts true the instant
  // the deferred value reaches what was actually committed, with no second
  // press needed.
  // Seeded already-open, once, for a query the navigation that landed on this
  // URL already committed (navHintQCommitted, router.ts) — the file view's
  // merged field pushes here with a query it had already cleared its own
  // commit gate for, and asking this page's box to clear it again would be
  // the second Enter that navigation exists to avoid. Any other mount
  // (a fresh load, a typed URL, a plain in-folder navigation) has no such
  // hint and starts closed exactly as before.
  const committedGate = useRef<string | null>(
    urlSync && navHintQCommitted() ? currentQuery().trim() : null,
  );
  const [gateNonce, setGateNonce] = useState(0);
  const gateOpen = !escapes || committedGate.current === q;
  const commitSearch = () => {
    const live = query.trim();
    if (committedGate.current === live) return;
    committedGate.current = live;
    setGateNonce((n) => n + 1);
  };

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

  // --- the on-demand scan episode ---------------------------------------------
  //
  // A scan has been asked for, for this folder and generation, and how many
  // answers have arrived since. Both reset with the folder or the generation.
  const asked = useRef(false);
  const sinceAsk = useRef(0);
  const polls = useRef(0);
  // Bumped by the poll timer to re-ask while a scan is running.
  const [pollTick, setPollTick] = useState(0);
  const [polling, setPolling] = useState(false);
  // Which folder+generation the async replies below still speak for. A scan
  // ask outlives the effect that issued it and is not usefully abortable
  // (aborting the fetch would not stop a scan the server has already
  // started), so it is tagged instead: a reply from before a navigation or a
  // reconcile is dropped rather than applied to whatever the box is showing
  // now. This hook is NOT remounted per folder (only the embedded listing is
  // keyed on its path), so there is no mount boundary doing this for us.
  const sourceEpoch = useRef(0);
  useEffect(() => {
    sourceEpoch.current += 1;
    asked.current = false;
    sinceAsk.current = 0;
    polls.current = 0;
    setPolling(false);
  }, [fsPath, pinned]);

  // --- the ranked answer -------------------------------------------------------
  const [answer, setAnswer] = useState<RankAnswer | null>(null);
  const [failure, setFailure] = useState("");
  const [pending, setPending] = useState(false);
  // Armed the moment the fetch effect below commits to a debounced request —
  // gate open, nothing served from the memo — rather than when that request
  // actually goes out. `pending` above answers "is a round trip in flight",
  // which is also what drives the spinner and the heavy dim, and those are
  // rightly quiet during the debounce wait itself. This answers the narrower
  // question the caveat's guard actually needs: "is an answer coming at all,
  // including the wait before the round trip starts."
  const [requestComing, setRequestComing] = useState(false);
  const memo = useRef(new QueryMemo<RankAnswer>());
  const inflight = useRef<AbortController | null>(null);
  // Identity of the request in flight (folder, generation, attempt, query), or
  // null. See the guard in the fetch effect.
  const inflightKey = useRef<string | null>(null);
  const answerSeq = useRef(0);
  // The generation the ranked answer on screen was fetched under.
  //
  // A lifecycle bump — a scan completed, or the index was deleted — RE-ASKS
  // (it is a few KB and it never blanks the list). A dir-watch bump does not:
  // those are frequent and the deferral is what keeps a churny folder
  // readable. The answer carries the generation it was fetched under, so the
  // caption is about THIS answer rather than about a counter that has since
  // moved.
  const answerGen = useRef(gen);
  // Whether an answer has EVER been recorded for the current search episode —
  // set at the same two places `answerGen.current` is (a memo hit, a fetch
  // landing), cleared wherever the answer itself is cleared. `generationBehind`
  // below needs this: `answerGen.current` starts equal to `gen` at mount, but
  // a later bump (of `gen`, for reasons that have nothing to do with this
  // search) makes them differ even when no answer was ever fetched — and "not
  // refreshed" is a claim about an EXISTING answer, meaningless with none on
  // screen.
  const answered = useRef(false);
  // ...read from a ref, never from the effect's closure: making `gen` a
  // dependency of the fetch would re-issue the request on every dir-watch
  // bump and every completed scan, which is the churn listing/revalidate
  // exists to refuse.
  const genRef = useRef(gen);
  genRef.current = gen;
  // The index MOVING makes every remembered answer suspect at once — that is
  // the memo's whole coherence story (platform/lib/instant-search). Only the
  // memo goes here: the rows on screen stay, and are replaced when the re-ask
  // lands, because a completed scan is not a reason to blank the list.
  useEffect(() => {
    // `rankedPref` too: a memoized answer from before the preference was
    // toggled is in the WRONG order, not merely stale.
    memo.current.clear();
  }, [fsPath, pinned, lifecycle, rankedPref]);
  // A new folder or an adopted generation, on the other hand, drops the answer
  // outright: those rows are about something else.
  useEffect(() => {
    setAnswer(null);
    answered.current = false;
    setFailure("");
    committedGate.current = null;
  }, [fsPath, pinned]);
  useEffect(() => () => inflight.current?.abort(), []);

  // What to do with an answer: render it, ask for a scan, or poll. The
  // decision itself is pure (listing/index-source); this is the wiring for it.
  const applyStep = (res: IndexRankResult, epoch: number) => {
    if (asked.current) sinceAsk.current += 1;
    const step = nextStep({
      reason: res.reason ?? "",
      asked: asked.current,
      sinceAsk: sinceAsk.current,
      polls: polls.current,
    });
    if (step === "scan") {
      asked.current = true;
      sinceAsk.current = 0;
      setPolling(true);
      // A refusal is durable — mount-backed, gone, or scanned too recently to
      // scan again (server/routers/index.py). Nothing is coming, so stop
      // polling and settle for whatever the next answer says. Tagged with the
      // epoch, because this reply can land on a folder the box has since
      // navigated away from.
      // `res.base` is the folder the answer is actually about — the base a
      // `~`/`/`-leading query resolved to (RankAnswer.base's own doc), which
      // can differ from `fsPath` (the folder currently open). Scanning
      // `fsPath` for an answer that came from elsewhere scans a folder
      // nothing asked about and leaves the real target unindexed. Falling
      // back to `fsPath` when `res.base` is empty keeps the request pointed
      // at a real folder rather than sending an empty root to the server.
      void requestFolderScan(res.base || fsPath).then(
        (r) => {
          if (sourceEpoch.current !== epoch) return;
          if (!r.started) setPolling(false);
        },
        () => {
          if (sourceEpoch.current !== epoch) return;
          setPolling(false);
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
  //
  // `!runsSearch` covers two cases the same way: nothing typed yet
  // (`!searching`), and a path-shaped query typed instead (`isPathQuery`) —
  // the completion dropdown (`useCompletion.ts`) already answers that one on
  // its own, cheaper, listing-based path, so no rank request goes out behind
  // it at all.
  useEffect(() => {
    if (!runsSearch) {
      inflight.current?.abort();
      inflightKey.current = null;
      setPending(false);
      setRequestComing(false);
      // Closing the box ENDS the episode, like every other exit from one.
      // Without this, escaping out of a search thirty ticks into a scan and
      // reopening it while that scan still runs handed the next search the
      // ticks the last one spent (index-source's ceiling is per episode).
      polls.current = 0;
      setPolling(false);
      // Leaving search drops the answer. Keeping it would carry one search
      // session's rows into the next one's first frame under a query they
      // have nothing to do with. The memo keeps the round trip cheap if the
      // same query comes back.
      setAnswer(null);
      answered.current = false;
      return;
    }
    if (!gateOpen) {
      // Decision 4: waiting for Enter. Nothing is asked, and whatever answer
      // is already on screen (for the last COMMITTED query) simply stays —
      // it is already flagged stale by the existing query-mismatch check
      // below (`staleRows`), which is the same dimming a keystroke under
      // debounce gets.
      inflight.current?.abort();
      inflightKey.current = null;
      setPending(false);
      setRequestComing(false);
      return;
    }
    // While a scan is running the remembered answer is exactly the one that is
    // out of date, so the memo is consulted only when nothing is on its way.
    const remembered = polling ? undefined : memo.current.get(q);
    if (remembered) {
      inflight.current?.abort();
      inflightKey.current = null;
      // The rows come back with their own generation, so the caption is about
      // THESE rows rather than about the last request that happened to run.
      answerGen.current = remembered.gen;
      answered.current = true;
      setAnswer(remembered);
      setFailure("");
      setPending(false);
      setRequestComing(false);
      return;
    }
    // The request this run would issue. Compared against the one already out,
    // so a poll tick does not abort a live request and start it again: a rank
    // that outlasts SCAN_POLL_MS would otherwise never be allowed to finish.
    const key = [fsPath, pinned, lifecycle, retryNonce, q, rankedPref].join(" ");
    if (inflightKey.current === key) return;
    // Past every early return above: this effect run WILL issue a
    // request, once the debounce below elapses. Armed here, at
    // scheduling, not inside `run` where the round trip actually starts
    // — the caveat's guard needs to cover the wait too, not just the flight.
    setRequestComing(true);
    const run = () => {
      inflight.current?.abort();
      inflightKey.current = key;
      const ctl = new AbortController();
      inflight.current = ctl;
      // The epoch AT ISSUE TIME, and this is the request that most needed it.
      const epoch = sourceEpoch.current;
      setPending(true);
      // The previous failure is not this request's verdict.
      setFailure("");
      // Same disambiguation the server uses (resolve_query: mode is `"*" in
      // raw`, unconditionally) — asked here only to pick how many rows are
      // worth fetching before the answer says which mode actually ran.
      const limit = q.includes("*") ? SEARCH_GLOB_RANK_LIMIT : SEARCH_RANK_LIMIT;
      // Decision 10: measured at issue, applied at the response — the same
      // two endpoints home-search.ts's `elapsedMs` uses, so the two boxes
      // report the same kind of number.
      const issuedAt = Date.now();
      indexRank(fsPath, q, { signal: ctl.signal, limit, ranked: rankedPref }).then(
        (res) => {
          if (ctl.signal.aborted || sourceEpoch.current !== epoch) return;
          inflightKey.current = null;
          const step = applyStep(res, epoch);
          answerSeq.current += 1;
          answerGen.current = genRef.current;
          answered.current = true;
          const next: RankAnswer = {
            query: q,
            gen: genRef.current,
            hits: hitsFromRank(res.hits, q, res.mode),
            truncated: res.truncated,
            total: res.total,
            reason: res.reason ?? "",
            base: res.base,
            mode: res.mode,
            elapsedMs: Date.now() - issuedAt,
          };
          // Remembered only once nothing is on its way to change it: an answer
          // taken mid-scan is a snapshot of a folder still being indexed.
          if (remembersAnswer(step, res.reason ?? "")) memo.current.put(q, next);
          setAnswer(next);
          setFailure("");
          setPending(false);
          setRequestComing(false);
        },
        (err: Error) => {
          if (ctl.signal.aborted || err.name === "AbortError") return;
          if (sourceEpoch.current !== epoch) return;
          inflightKey.current = null;
          // The rows in hand STAY (see the header); the error only reaches the
          // screen when there is nothing else to show.
          setFailure(err.message);
          setPending(false);
          setRequestComing(false);
        },
      );
    };
    // Trailing debounce: this effect re-runs on every dep change below and
    // its cleanup clears the pending timer, so an unconditional wait resets
    // on each keystroke and only a pause fires the request.
    const timer = window.setTimeout(run, INSTANT_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- applyStep is
    // recreated each render; everything it reads is a ref or listed here.
  }, [fsPath, q, searching, isPathQuery, pinned, lifecycle, retryNonce, pollTick, polling, rankedPref, gateNonce]);

  // The poll itself: while a scan covering this folder is running, ask again
  // on a modest cadence and repaint. The ordering WILL shift as rows land;
  // what must not happen is the list going empty between repaints, and it
  // cannot — the previous answer stays until the next one replaces it.
  //
  // The TICK is what is counted against the ceiling, not the answers — see
  // listing/index-source's MAX_SCANNING_POLLS.
  useEffect(() => {
    if (!polling || !searching) return;
    const timer = window.setTimeout(() => {
      polls.current += 1;
      const step = nextStep({
        reason: "scanning",
        asked: asked.current,
        sinceAsk: sinceAsk.current,
        polls: polls.current,
      });
      if (step === "poll") {
        setPollTick((n) => n + 1);
        return;
      }
      // Out of patience — the same rule the answer path uses, so there is one
      // definition of what running out means (listing/index-source). The
      // count is per polling EPISODE: leaving it armed would make the next
      // scan of this folder give up on its first tick.
      polls.current = 0;
      setPolling(false);
    }, SCAN_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [polling, searching, pollTick]);

  // First focus warms the answer in the background; focus (like typing below)
  // is also the retry gesture after a failure.
  //
  // Focus is deliberately NOT a revalidation boundary. It reads like one — the
  // user is "coming back to" the search — but it is ambient: the pane focus
  // guard, a split remount at a width threshold, and WebKit restoring focus
  // after a repaint all fire it with no gesture behind them. Treating it as a
  // boundary adopted whatever churn had accumulated and swapped the results
  // out from under someone who was reading them, which is the exact thing the
  // deferral exists to prevent.
  const probed = useRef("");
  const prefetchIndex = () => {
    if (failure !== "") setRetryNonce((n) => n + 1);
    // One cheap probe per folder+generation. It pays the server's cold cost
    // (the duckdb import, the gitignore pool) before the first keystroke.
    const probeKey = fsPath + ":" + pinned;
    if (probed.current === probeKey) return;
    probed.current = probeKey;
    void indexRank(fsPath, "", { limit: 1 }).then(() => {}, () => {});
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
    // A query change is deliberately NOT a revalidation boundary: adopting
    // the latest generation on every keystroke would invalidate everything
    // in hand and re-run the fetch, so any keystroke that arrives after
    // background churn would pay for a generation nobody asked to move to.
    // Being a generation behind is a state this search can simply live in
    // (listing/revalidate).
    //
    // Editing the query is still a user gesture, so it is still the retry for
    // a failed request: otherwise search stays dead until something else moves.
    if (failure !== "") setRetryNonce((n) => n + 1);
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

  // --- what the box hands over -------------------------------------------------
  //
  // The ranked answer is rendered whatever query it answers (see the header),
  // so `staleRows` is how the caller learns to dim it.
  const hits = searching ? (answer?.hits ?? []) : [];
  // The directory `hits`' `rel`s are relative to — `fsPath` whenever nothing
  // is searching, else whatever `res.base` the server actually resolved the
  // query against (see `RankAnswer.base`). A row path built from `entry.rel`
  // must join onto THIS, never onto `fsPath` directly, once searching.
  //
  // The gap is the box's very first request: `answer` is still `null`
  // (nothing has ever landed for this box) while a request for a query like
  // `~/Work/*/*.json` is already out. `fsPath` — the folder already open —
  // is not that request's base; it is exactly what a leading `~`/`/` is
  // written to escape. Reporting it here would be the header asserting a
  // base this search does not have yet, so this falls back to the empty
  // string instead, which the header's own rendering already treats as
  // "nothing known yet" (a bare "Path", no base named). Once ANY answer has
  // landed — including a stale one still answering an EARLIER query while a
  // newer request is out, which `staleRows` below flags separately — its
  // `base` is real and gets named again; a query that never leaves `fsPath`
  // resolves to `fsPath` itself once it lands, so it is never stuck showing
  // nothing. A path-shaped query (`isPathQuery`, no rank request ever goes
  // out) never gets an answer either, so it falls in with "not searching"
  // here too — the resting `fsPath` base, not a permanently-empty one.
  const searchBase = !runsSearch
    ? fsPath.replace(/\/$/, "")
    : answer !== null
      ? answer.base
      : "";
  const staleRows = answer !== null && answer.query !== q;
  const displayHits = hits;

  // Whether the rendered rows are an ANSWER to the query in the box.
  //
  // The ranked path can show rows for a query the user has typed past,
  // because it deliberately never blanks the list while the next answer is
  // in flight — so it has to SAY so, and the sayer is this flag. It gates the
  // guesses the listing makes on the user's behalf: auto-selecting the top
  // hit (listing/selection) and Enter opening row 0 with nothing selected
  // (useListingSelection). Both would otherwise act on a file that answers
  // nothing the user typed.
  //
  // The question is about the ROWS, never about a request being out:
  // gating on `pending` would flip this false for the length of every round
  // trip, so a poll tick during a scan would make the listing withdraw its
  // selection and re-place it every tick. `deferredStale` is the other half:
  // `q` trails the input by a commit under load, so there is a render where
  // the rows answer a query the user has already typed past while nothing is
  // in flight at all.
  //
  // `!searching`, NOT `!runsSearch`: an empty box is the one case where
  // "rows" (the folder's own, via `navRows`/`showsSearchHits` in Listing.tsx)
  // are trivially the answer — there is no query to answer. A path-shaped
  // query is `searching` (there IS a query) but `runsSearch` is false (no
  // rank request is ever issued for it), and its rows are the same folder
  // listing, which does NOT answer an arbitrary typed path. Folding that case
  // into the `!runsSearch` shortcut (as an earlier version of this did) made
  // `rowsAnswerQuery` true for every path-shaped query, and the document
  // Enter handler (useListingSelection.ts) reads exactly this flag to decide
  // whether opening `rows[0]` with nothing selected is a safe guess — so it
  // opened an arbitrary, unrelated folder row on Enter (code review finding
  // 1). `runsSearch` is still what decides staleness for an actual search.
  const rowsAnswerQuery = !searching || (runsSearch && !staleRows && !deferredStale);

  // The mode the LAST settled answer actually ran under — "substring" once
  // nothing has searched yet, since that is the cap capHits already defaults
  // to.
  const mode = answer?.mode ?? "substring";

  // The rendered rows: the top SEARCH_RESULT_CAP, for either query shape
  // (listing/result-cap) — a glob's matches are all equally relevant, but a
  // broad enough pattern can still return thousands of them, which is the
  // same "too many rows for a screen" problem the substring cap already
  // solves. This is also what keyboard nav and auto-select walk, so they
  // never address a row that is not on screen.
  const visibleHits = useMemo(() => capHits(displayHits, mode), [displayHits, mode]);

  // How many ranked matches the cap is hiding — the counter reports the true
  // total and tells the user to narrow the query (listing/result-cap).
  const cappedAway = displayHits.length - visibleHits.length;

  // The search's settled state, in the shape the rendering keys off
  // (listing/types SearchState): idle outside search, pending with nothing to
  // show yet, a failure with nothing to show, or a settled answer carrying
  // the truncation the count chip owns up to.
  //
  // `!runsSearch`, not `!searching`: a path-shaped query is exactly as idle
  // as an empty box, since no request was ever issued for it and none ever
  // will be. Without this a committed path-shaped query (`gateOpen` true,
  // `answer` permanently `null`) would fall through to the `pending` branch
  // below and sit there forever — a spinner for a request that will never
  // exist.
  const searchState: SearchState = !runsSearch
    ? IDLE_SEARCH
    : !gateOpen && answer === null
      // Decision 4: nothing has ever been asked for this query yet, and
      // nothing is coming until Enter — the same "no request, no verdict"
      // shape as the empty box, not a settled empty answer.
      ? IDLE_SEARCH
      : failure !== "" && displayHits.length === 0
      ? { status: "error", message: failure, forRefresh: pinned }
      : answer === null
        ? { status: "pending", forRefresh: pinned }
        : {
            status: "ok",
            truncated: answer.truncated,
            total: answer.total,
            forRefresh: pinned,
            elapsedMs: answer.elapsedMs,
          };

  // Two questions, not one (listing/index-source): whether an answer is still
  // coming, and whether the wait is the momentary kind. A scan landing rows is
  // the first without being the second.
  const progress = searchProgress({ searching, pending, polling });
  const scanPending = progress.answerComing;
  // Momentary states: a request in flight, or a deferred value that has not
  // caught up. Both get the heavy dim, calibrated for something that clears
  // in a moment — so a scan running for a minute is deliberately NOT one of
  // them; it has the "indexing…" caveat instead.
  const isStale = deferredStale || progress.inFlight;
  // Being a generation behind is NOT momentary: the folder or the index moved
  // and this search deliberately did not follow, and it will stay that way
  // until a real boundary (listing/revalidate). Requires `answered.current`:
  // with no answer ever recorded for this search, `answerGen.current` and
  // `gen` differing says nothing about staleness — there is no existing
  // answer for "not refreshed" to describe.
  const generationBehind = searching && answered.current && answerGen.current !== gen;

  // A settled failure with rows still on screen: the last request for the
  // CURRENT query errored, so `hits` is whatever an earlier query answered,
  // not this one. `displayHits.length > 0` is what keeps this out of the
  // zero-hit case — a failure with nothing on screen already reports
  // `status: "error"` above and needs no caveat, since there is nothing to
  // caption as an answer to something else. `failure` is cleared the
  // instant a new request goes out (this file's `run`), so this can never
  // be true at the same time as `pending`/`requestComing`.
  const requestFailed = failure !== "" && displayHits.length > 0;

  // The rows on screen answer the last COMMITTED query, not the one now in
  // the box: the query names a different base (`escapesBase`,
  // listing/query-base.ts) and Enter has not been pressed for it yet. This
  // is NOT staleness — nothing failed to refresh, nothing was asked for this
  // query — so it gets its own name rather than folding into
  // `generationBehind` above, which is what "not refreshed" actually
  // describes. Gated on `!gateOpen` explicitly (rather than left to fall out
  // of `staleRows` alone) so this stays true by inspection rather than by
  // re-deriving the gate's reachability argument, and survives a future
  // change to the gate.
  const awaitingCommit = searching && !gateOpen && staleRows && !pending;

  // No spinner flash: a pending indicator appears only once being pending is
  // information rather than a flicker. The common answer lands well inside
  // PENDING_INDICATOR_MS.
  const unsettled = runsSearch && (scanPending || searchState.status === "pending");
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
    // Path vs. Search — see path-shaped-query.ts. The one predicate every
    // caller reads for the chip, the enter banner, the footer, AND (above)
    // whether this hook ever asks the index anything at all.
    isPathQuery,
    isStale,
    // Decision 4: a query whose base escapes the box root waits for this
    // before it fetches.
    escapes,
    gateOpen,
    commitSearch,
    // "These results are computed from an older generation of the tree, or for
    // a query that has moved on, and nothing is on its way to fix that" — see
    // above. Drives the caveat chip and its own, lighter dim. A settled
    // failure with rows on screen (`requestFailed`) folds in here too: it is
    // the same shape ("no answer is coming for the current query, these rows
    // are from an earlier one, and it is staying that way until a boundary")
    // even though the reason is a rejected request rather than a generation
    // bump — the caveat text itself is what tells the two apart.
    behind: generationBehind || requestFailed,
    // Whether the failed request is what makes `behind` true above, as
    // opposed to a generation bump — the caveat (listing/index-caveat.ts)
    // needs this distinction because "not refreshed" is the wrong claim for
    // rows sitting behind a request that already tried and errored.
    requestFailed,
    // "The query in the box names a different base than these rows answer,
    // and Enter has not been pressed for it" — see above. Tells the caller
    // to replace the rows with the Enter prompt rather than caption them.
    awaitingCommit,
    scanPending,
    // Armed from the moment a request is scheduled, not from when it goes
    // out — the caveat's own `pending` guard (index-caveat.ts's
    // `searchCaveat`) needs this rather than `scanPending`, which only turns
    // true once the round trip is actually in flight and would leave the
    // debounce wait itself uncovered.
    requestComing,
    /** Whether an answer is still coming AND has taken long enough to say so. */
    spinner: unsettled && slow,
    // This app changed a file and the rescan it triggered has not landed yet
    // (server/index_touch.py). Read during render, and re-read whenever it can
    // have moved: both signals that change it are subscribed above.
    rescanPending: indexRescanPending(),
    searchState,
    // The server's reason for the LAST settled answer, "" when it simply
    // answered. The caller turns a non-empty one into the index gap
    // (lib/home-search's `indexGap`) — the client holds no copy of the rules
    // behind it.
    reason: answer?.reason ?? ("" as RankReason),
    prefetchIndex,
    hits,
    searchBase,
    displayHits,
    visibleHits,
    rowsAnswerQuery,
    cappedAway,
    mode,
  };
}
