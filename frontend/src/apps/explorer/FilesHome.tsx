// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useEffect, useMemo, useRef, useState } from "react";
import { navigate, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { basename, formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config, ClaudeSessionFolder, GitRepos, IndexStatus } from "@platform/lib/api";
import { indexCaveat } from "@apps/explorer/listing/index-caveat";
import { getClaudeSessionFolders, getGitRepos, indexSearch, statPath } from "@platform/lib/api";
import { allBookmarks, hydrateBookmarks, loadBookmarks } from "@platform/lib/bookmarks";
import { useBookmarksVersion, useUrlVersion } from "@platform/lib/hooks";
import {
  fsMutationCount,
  indexLifecycleCount,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { hydrateRecents, loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { BookmarkPreviewCard, RecentPreviewCard, FolderPreviewCard } from "@apps/explorer/BookmarkCards";
import { describeSpec, runAiSearch, type AiSearchResult } from "@apps/explorer/lib/ai-search";
import {
  refreshIsPending,
  reposMessage,
  reposNeedsIndexPoll,
  reposStaleNote,
  reposView,
} from "@apps/explorer/lib/repos";
import { useIndexStatus } from "@platform/lib/index-status";
import {
  INSTANT_DEBOUNCE_MS,
  activeRow,
  corpusFrom,
  homeCorpusView,
  homeCountNote,
  homeHitsFrom,
  isAiRow,
  nextHeldHomeCorpus,
  pathShortcut,
  rankingSettled,
  redirectsToSearch,
  stepHighlight,
  submitRow,
  type CorpusState,
  type HeldHomeCorpus,
  type HomeHit,
} from "@apps/explorer/lib/home-search";
import { useRankedScan } from "@apps/explorer/listing/useRankedScan";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// How many cards a tab shows before "Show more" — flat count, not a row
// multiple, so it's the same rule for every tab regardless of how many columns
// the grid happens to lay out at the current width.
// 9 fills a 3×3 grid at the layout's usual three columns.
const MAX_CARDS = 9;

type LaunchTab = "bookmarks" | "recents" | "sessions" | "repos";

// -- The home search bar ------------------------------------------------------
//
// A plain file search that answers while you type, with AI search as one row
// at the bottom of the results rather than the only way in. Typing ranks the
// home root's index corpus locally (lib/home-search); picking the last row —
// "Search with AI" — spends the model call the old composer spent on every
// Enter, including on queries that were just a filename.
//
// The two modes never blur into each other: AI results REPLACE the instant
// list (with the spec echo that makes a wrong interpretation visible), and
// editing the query drops back to instant results. Only a committed AI search
// touches the URL — mirroring every keystroke into ?q= would fill the history
// with half-typed words and re-run a model call on reload.

// What the AI half of the box is doing. `off` is the normal state: the results
// are the index's, and the AI row is an offer.
type AiPhase =
  | { status: "off" }
  | { status: "running"; query: string }
  | { status: "done"; query: string; result: AiSearchResult }
  | { status: "failed"; query: string; message: string };

const AI_OFF: AiPhase = { status: "off" };

function MagnifierIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7" />
      <path d="M21 21l-4.3-4.3" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 3l1.8 4.9L18.7 9.7l-4.9 1.8L12 16.4l-1.8-4.9L5.3 9.7l4.9-1.8L12 3z" />
      <path d="M18 16.5l.7 1.8 1.8.7-1.8.7-.7 1.8-.7-1.8-1.8-.7 1.8-.7.7-1.8z" />
    </svg>
  );
}

// One file hit. An <a> (not a button) so cmd/ctrl-click opens it the way every
// other path in this app does.
function FileRow({
  hit,
  home,
  active,
  id,
  onHover,
}: {
  hit: HomeHit;
  home: string;
  active: boolean;
  id: string;
  onHover: () => void;
}) {
  const display = hit.path.startsWith(home + "/") ? "~/" + hit.rel : hit.path;
  return (
    <li role="option" id={id} aria-selected={active}>
      <a
        className={"fh-result" + (active ? " is-active" : "")}
        href={urlForFsPath(hit.path)}
        title={hit.path}
        onMouseMove={onHover}
        onClick={(e) => {
          if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
            return;
          e.preventDefault();
          navigate(hit.path, { isDir: hit.is_dir });
        }}
      >
        <span className="fh-result-icon" aria-hidden="true">
          {iconForEntry(basename(hit.path), hit.is_dir)}
        </span>
        <span className="fh-result-name">{basename(hit.path)}</span>
        <span className="fh-result-path">{display}</span>
        <span className="fh-result-meta">
          {hit.is_dir ? "" : formatSize(hit.size)}
          {hit.mtime !== null && (
            <span className="fh-result-date" title={formatMtimeFull(hit.mtime)}>
              {formatMtime(hit.mtime)}
            </span>
          )}
        </span>
      </a>
    </li>
  );
}

// The last row: an ACTION, not a hit. Deliberately unlike the file rows above
// it (accent glyph, no path, no size) because activating it costs a model call
// and a wait, and because on a zero-hit query it is the only thing on screen.
//
// Deliberately NOT hoverable, unlike the file rows: setting the highlight on
// mousemove meant nudging the pointer across the list armed Enter to spend a
// model call. A pointer merely crossing a row must not arm a paid action —
// reaching this one takes an arrow key or a click.
function AiActionRow({
  query,
  active,
  running,
  id,
  onRun,
}: {
  query: string;
  active: boolean;
  running: boolean;
  id: string;
  onRun: () => void;
}) {
  return (
    <li role="option" id={id} aria-selected={active}>
      <button
        type="button"
        className={"fh-result fh-ai-row" + (active ? " is-active" : "")}
        disabled={running}
        onClick={onRun}
      >
        <span className="fh-result-icon fh-ai-glyph" aria-hidden="true">
          <SparkIcon />
        </span>
        <span className="fh-result-name">Search with AI</span>
        <span className="fh-result-path">“{query}”</span>
        <span className="fh-result-meta">
          {running ? "Asking…" : <kbd>↵</kbd>}
        </span>
      </button>
    </li>
  );
}

// The "Understood as: …" echo above AI results — the one place a wrong
// interpretation becomes visible instead of silently shaping the list.
function AiResults({ home, query, result }: { home: string; query: string; result: AiSearchResult }) {
  const summary = describeSpec(result.spec);
  return (
    <div className="fh-panel">
      <p className="fh-search-summary">
        <span className="fh-ai-badge">
          <SparkIcon /> AI
        </span>
        {summary ? `Understood as: ${summary}` : "No filters — showing closest matches."}
        {result.truncated && " · Broad query: showing the first slice of matches."}
      </p>
      {result.hits.length ? (
        <ul className="fh-results" role="listbox" aria-label="AI search results">
          {result.hits.map((h) => (
            <FileRow
              key={h.path}
              home={home}
              active={false}
              id={"fh-ai-hit-" + h.path}
              onHover={() => {}}
              hit={{
                path: h.path,
                rel: h.path.startsWith(home + "/") ? h.path.slice(home.length + 1) : h.path,
                is_dir: h.is_dir,
                size: h.size,
                mtime: h.mtime,
              }}
            />
          ))}
        </ul>
      ) : (
        <p className="fh-empty">
          AI search found nothing for “{query}”. Try different words, or fewer of them.
        </p>
      )}
    </div>
  );
}

function FilesSearch({
  home,
  initialQuery,
  indexScan,
  onActiveChange,
}: {
  home: string;
  initialQuery: string;
  /** The shared index poll (see FilesHome) — this box adds no poller of its own. */
  indexScan: IndexStatus | null;
  onActiveChange: (active: boolean) => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [ai, setAi] = useState<AiPhase>(AI_OFF);
  const [highlight, setHighlight] = useState<number | null>(null);
  const q = query.trim();
  const active = q !== "";
  useEffect(() => onActiveChange(active), [active, onActiveChange]);

  // -- the corpus ------------------------------------------------------------
  // Fetched ONCE per index generation, not per keystroke: the index answers
  // with the whole covered subtree, so re-asking on every letter would spend a
  // round trip to receive the same rows. Ranking is what runs per query.
  // A scan finishing or the index being deleted changes what the corpus IS,
  // and no other signal reports it (the filesystem did not change) — see
  // lib/index-freshness.
  const [lifecycle, setLifecycle] = useState(indexLifecycleCount);
  useEffect(() => subscribeIndexLifecycle(() => setLifecycle(indexLifecycleCount())), []);
  // ...but it is RECORDED, not applied. Scans complete often, and refetching on
  // each one swapped the rows out from under whoever was reading them — the
  // exact churn this page is being fixed to stop. A corpus in hand therefore
  // pins the fetch generation and keeps answering, captioned "indexing…" and
  // dimmed; only having nothing to lose lets the fetch follow the index, which
  // is precisely the case this signal was added for (the first scan finishing
  // while the page sits on "still building"). The same posture the in-folder
  // search takes for its dir-watch bumps (listing/revalidate).
  const [fetchLifecycle, setFetchLifecycle] = useState(lifecycle);
  const [corpus, setCorpus] = useState<CorpusState>({ status: "idle" });
  useEffect(() => {
    // Two ways the pin comes off, and between them they are what stop it from
    // being a one-way door — pinned on the session's first corpus with nothing
    // to ever move it, the box would rank an hour-old corpus and caption it
    // "not refreshed" forever.
    //
    //   * there is nothing to lose (no corpus yet), which is the first-scan
    //     case this signal was added for;
    //   * THE SEARCH ENDED. Nothing is on screen to be pulled out from under
    //     anyone, so adopting costs the user nothing and the next search opens
    //     on current data. This is exactly the boundary the in-folder search
    //     already reconciles at (revalidate.shouldReconcile returns true the
    //     moment `searching` goes false), and the two boxes should not disagree
    //     about when a search is over.
    if (corpus.status !== "ok" || !active) setFetchLifecycle(lifecycle);
  }, [lifecycle, corpus.status, active]);
  // Which generation the corpus IN HAND actually reflects, which is not the
  // same question as when the fetch is allowed to re-run: a refetch forced by
  // something else (an in-app rename, a retry) still comes back with current
  // data and has to clear this, or the caveat would stick forever. Stamped
  // from the moment the request was ISSUED, so a scan that completes while it
  // is in flight leaves the answer marked behind rather than falsely current.
  const corpusLifecycle = useRef(lifecycle);
  const corpusBehind = corpusLifecycle.current !== lifecycle;
  // An in-app rename/delete moves paths the fetched corpus already holds, so
  // search would find the old name and the click would 404. It is a REFETCH
  // trigger, not a gate: `indexMayAnswer(home)` used to disable instant search
  // outright, and since `touched` is session-scoped and home is an ancestor of
  // every mutation, one rename anywhere pinned this page to "still building"
  // for the rest of the session — while the index was in fact built. The home
  // page has no live walk to fall back on, so switching search OFF is the worst
  // available outcome: a corpus one rename stale beats no corpus at all.
  // (useWalkSearch keeps the gate: it has a live walk that can answer the
  // renamed folder correctly, and racing the index against that walk does not
  // change the calculus — the index would win the race with the wrong name.)
  const [mutations, setMutations] = useState(fsMutationCount);
  useEffect(() => subscribeFsMutations(() => setMutations(fsMutationCount())), []);
  // Bumped by a real gesture (typing) to re-run a failed fetch. Without it a
  // `setCorpus({status:"error"})` was terminal: none of the other deps is
  // something a user can move, so search stayed dead until a reload.
  const [retryNonce, setRetryNonce] = useState(0);
  // Requested on the first keystroke and never unrequested: dropping the
  // corpus when the box is cleared would re-fetch it on the next letter typed.
  const [wanted, setWanted] = useState(active);
  useEffect(() => {
    if (active) setWanted(true);
  }, [active]);
  useEffect(() => {
    if (!wanted) return;
    const ctl = new AbortController();
    const issuedAt = indexLifecycleCount();
    setCorpus((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    indexSearch(home, { signal: ctl.signal }).then(
      (res) => {
        if (ctl.signal.aborted) return;
        corpusLifecycle.current = issuedAt;
        setCorpus(corpusFrom(res));
      },
      (err: Error) => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        setCorpus({ status: "error", message: err.message });
      },
    );
    return () => ctl.abort();
  }, [home, wanted, fetchLifecycle, mutations, retryNonce]);

  // A corpus once in hand keeps answering while the next one is fetched. The
  // rescan that republishes this fetch used to put the box back into `cold`
  // for its duration — "The file index is still building", zero rows, for an
  // index that was built. Only never having had a corpus may suppress rows;
  // being a generation behind is a note, not a downgrade (lib/home-search,
  // and lib/repos.ts for the same rule on the repo cards).
  const heldCorpus = useRef<HeldHomeCorpus | null>(null);
  heldCorpus.current = nextHeldHomeCorpus(corpus, heldCorpus.current);
  const view = homeCorpusView(corpus, heldCorpus.current);
  // Behind either because the fetch is deliberately pinned to an older index
  // generation, or because the rows on screen are the held ones while a fetch
  // the user's own action forced actually runs.
  const behind = view.entries !== null && (corpusBehind || view.stale);

  // -- ranking ---------------------------------------------------------------
  // The same sliced, cancellable scan the in-folder search runs — literally the
  // same hook (listing/useRankedScan): a covered home root can be 200k entries,
  // and scoring that synchronously on a keystroke is the typing freeze
  // listing/scan-job exists to prevent.
  const { ranked, pending } = useRankedScan(view.entries, q, INSTANT_DEBOUNCE_MS, view.key);
  const hits = useMemo(() => homeHitsFrom(ranked, home), [ranked, home]);
  const scanning = active && pending;

  // -- the box is where typing goes ------------------------------------------
  //
  // This page exists to be typed into, so the caret starts here and STAYS
  // reachable: a stray click on the background, or arriving with the hands
  // already moving, must not cost a click on the input to recover. Any printable
  // keystroke aimed at nothing else focuses the box and lands in it.
  const inputEl = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    inputEl.current?.focus();
  }, []);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      const claim = redirectsToSearch({
        key: e.key,
        ctrlKey: e.ctrlKey,
        altKey: e.altKey,
        metaKey: e.metaKey,
        tagName: el?.tagName,
        isContentEditable: el?.isContentEditable === true,
        isSearchInput: el === inputEl.current,
      });
      // Focus, then let the event through UNHANDLED: the browser delivers this
      // same keystroke to the newly focused input, so the character is neither
      // dropped nor inserted twice (preventDefault + appending by hand would
      // fight the controlled value and lose the caret).
      if (claim) inputEl.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  // -- AI search -------------------------------------------------------------
  const aiCtl = useRef<AbortController | null>(null);
  // The path shortcut's stat, cancellable for the same reason: both outlive the
  // gesture that started them, and both act on the app when they land.
  const statCtl = useRef<AbortController | null>(null);
  useEffect(
    () => () => {
      aiCtl.current?.abort();
      statCtl.current?.abort();
    },
    [],
  );
  const syncQueryParam = (value: string | null) => {
    const params = new URLSearchParams(location.search);
    if (value) params.set("q", value);
    else params.delete("q");
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };
  const runAi = (target: string) => {
    if (!target) return;
    // Re-entry guard, the keyboard's half of the `disabled` that already
    // protects the mouse path: Enter on the AI row while a search is in flight
    // aborted and re-issued it, so three impatient presses were three billed
    // model calls. Editing the query is the way to change what is running (it
    // resets the phase), so a running search is never for a stale query.
    if (ai.status === "running") return;
    aiCtl.current?.abort();
    const ctl = new AbortController();
    aiCtl.current = ctl;
    setAi({ status: "running", query: target });
    runAiSearch(home, target, ctl.signal).then(
      (result) => {
        if (ctl.signal.aborted) return;
        setAi({ status: "done", query: target, result });
        // Only a committed AI search rides the URL, so a reload reproduces the
        // search the user paid for — instant results need no restoring.
        syncQueryParam(target);
      },
      (err: Error) => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        // No substituted keyword search: the index results underneath are
        // still on screen, and they are the honest fallback (lib/ai-search).
        setAi({ status: "failed", query: target, message: err.message });
      },
    );
  };

  const clear = () => {
    aiCtl.current?.abort();
    statCtl.current?.abort();
    setQuery("");
    setAi(AI_OFF);
    setHighlight(null);
    syncQueryParam(null);
  };

  const edit = (value: string) => {
    setQuery(value);
    setHighlight(null);
    // Editing the query retracts the address that was submitted from it, so a
    // stat still in flight for the old one must not navigate when it lands.
    statCtl.current?.abort();
    // Typing is a user gesture, so it is also the retry for a failed corpus
    // fetch — the same way useWalkSearch re-arms its stream from setQuery.
    if (corpus.status === "error") setRetryNonce((n) => n + 1);
    // Editing the query is how the user gets back from AI results to instant
    // ones, so it drops the AI phase — and the ?q= that a committed search set.
    if (ai.status !== "off") {
      aiCtl.current?.abort();
      setAi(AI_OFF);
      syncQueryParam(null);
    }
  };

  // A ?q= restored from the URL was a committed AI search, so it re-runs one.
  const ranInitial = useRef(false);
  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (initialQuery.trim()) runAi(initialQuery.trim());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const showingAi = ai.status === "done" && ai.query === q;
  // Whether the instant list is a finished answer. Gates the AI row's
  // pre-selection, so Enter during the corpus load or the scan debounce cannot
  // spend a model call on a query that was about to answer itself.
  const settled = rankingSettled(view.status, scanning);
  // The indexing caveat, same helper and same two messages as the listing's
  // search chip (listing/index-caveat) so the two boxes make the same claim in
  // the same words. It is the piece that makes a lagging answer read as
  // intentional: with rows on screen from a corpus a generation behind and
  // nothing saying why, the box just looks wrong. The rows themselves dim
  // while `view.stale`, the same treatment the listing gives held results.
  const caveat = active && !showingAi ? indexCaveat(indexScan, behind) : null;
  const current = activeRow(highlight, hits.length, settled);

  const openRow = (row: number) => {
    if (isAiRow(row, hits.length)) runAi(q);
    else navigate(hits[row].path, { isDir: hits[row].is_dir });
  };

  // Enter with no row chosen: a query that is really an ADDRESS goes straight
  // there (a pasted ~/Downloads is not a search); otherwise it commits the top
  // hit, or — only once ranking has settled on nothing — the AI row.
  const submit = async () => {
    if (highlight === null) {
      const target = pathShortcut(q, home);
      if (target !== null) {
        // A stat on a slow or network mount can outlive the intent behind it, and
        // navigating on a stale answer is the worst kind of wrong: it yanks the
        // user out of wherever they went next. Superseded by the next submit,
        // cancelled by clear()/unmount, and re-checked after the await — a
        // resolved-but-abandoned stat must not move anyone.
        statCtl.current?.abort();
        const ctl = new AbortController();
        statCtl.current = ctl;
        try {
          const st = await statPath(target, ctl.signal);
          if (ctl.signal.aborted) return;
          navigate(st.path, { isDir: st.is_dir });
          return;
        } catch {
          if (ctl.signal.aborted) return;
          // not a real path — treat it as a search query
        }
      }
    }
    const row = submitRow(highlight, hits.length, settled);
    if (row !== null) openRow(row);
  };

  return (
    <div className="files-search-wrap">
      <div className="files-search">
        <span className="files-search-icon" aria-hidden="true">
          <MagnifierIcon />
        </span>
        <input
          ref={inputEl}
          type="search"
          className="files-search-input"
          placeholder="Search your files — or paste a path like ~/Downloads"
          aria-label="Search your files"
          role="combobox"
          aria-expanded={active && !showingAi}
          aria-controls="fh-result-list"
          aria-activedescendant={
            active && !showingAi && current !== null ? "fh-row-" + current : undefined
          }
          autoComplete="off"
          spellCheck={false}
          value={query}
          onChange={(e) => edit(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown" || e.key === "ArrowUp") {
              if (!active || showingAi) return;
              e.preventDefault();
              setHighlight((h) => stepHighlight(h, hits.length, e.key === "ArrowDown" ? 1 : -1));
            } else if (e.key === "Enter") {
              e.preventDefault();
              void submit();
            } else if (e.key === "Escape" && active) {
              e.preventDefault();
              clear();
            }
          }}
        />
        {active && (
          <button type="button" className="files-search-clear" title="Clear search (esc)" onClick={clear}>
            Clear
          </button>
        )}
      </div>

      {ai.status === "failed" && <ErrorBanner>{ai.message}</ErrorBanner>}

      {!active ? null : showingAi ? (
        <AiResults home={home} query={ai.query} result={ai.result} />
      ) : (
        <div className="fh-panel">
          <p className="fh-result-note">
            {/* `view.status`, not `corpus.status`: a refetch (a rescan, an
                in-app rename) puts the FETCH back into loading/cold while the
                corpus we are still ranking sits in hand. Branching on the raw
                fetch state meant a mid-rescan search claimed the index was
                "still building" over the rows it was showing. */}
            {view.status === "cold" ? (
              // Never "no matches" for an index that has not been built: that
              // would blame the user's files for the app's state.
              "The file index is still building — AI search can answer in the meantime."
            ) : view.status === "error" ? (
              `The file index could not be searched: ${view.message}`
            ) : view.status !== "ok" || scanning ? (
              "Searching…"
            ) : ranked.length === 0 ? (
              `No file name matched “${q}” — AI search can look at dates, types and sizes.`
            ) : (
              <>
                {homeCountNote(ranked.length, view.truncated)}
                {" · "}
                <kbd>↑</kbd>
                <kbd>↓</kbd> to pick · <kbd>esc</kbd> to clear
              </>
            )}
            {caveat && (
              <span className="fh-index-chip" title={caveat.title}>
                {/* Only while a scan is actually running. The chip also carries
                    the "not refreshed" caveat, whose entire point is that NO
                    work is in flight — a spinner there asserts the opposite of
                    what the words next to it say. (Listing.tsx keeps its
                    spinner on walk status for the same reason: the spinner
                    tracks work, the caveat tracks trust.) */}
                {indexScan?.scanning && <span className="fh-index-spinner" aria-hidden="true" />}
                {caveat.note}
              </span>
            )}
          </p>
          <ul
            className={"fh-results" + (behind ? " is-stale" : "")}
            id="fh-result-list"
            role="listbox"
            aria-label="Search results"
          >
            {hits.map((hit, i) => (
              <FileRow
                key={hit.path}
                hit={hit}
                home={home}
                active={current === i}
                id={"fh-row-" + i}
                onHover={() => setHighlight(i)}
              />
            ))}
            <AiActionRow
              query={q}
              active={current === hits.length}
              running={ai.status === "running"}
              id={"fh-row-" + hits.length}
              onRun={() => runAi(q)}
            />
          </ul>
        </div>
      )}
    </div>
  );
}

// The fold's "Show more" — a quiet pill under the grid, shared by both tabs.
function ShowMoreButton({ expanded, onClick }: { expanded: boolean; onClick: () => void }) {
  return (
    <button type="button" className="fhb-more" onClick={onClick}>
      {expanded ? "Show less" : "Show more"}
      <svg
        width="12"
        height="12"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.2"
        strokeLinecap="round"
        strokeLinejoin="round"
        aria-hidden="true"
        style={expanded ? { transform: "rotate(180deg)" } : undefined}
      >
        <path d="M6 9l6 6 6-6" />
      </svg>
    </button>
  );
}

export default function FilesHome({ config }: { config: Config }) {
  // Same normalization every other config.home consumer applies (App.tsx,
  // Panel.tsx): backslashed on Windows, while search hits and path helpers
  // (basename, the "home + /" strip below) are forward-slash-only.
  const home = config.home.replace(/\\/g, "/");
  useBookmarksVersion();
  useRecentsVersion();
  // The active tab lives entirely in the URL (?tab=recents) — read fresh on
  // every render, re-triggered by any history write (typed url, back/forward,
  // or this file's own tab clicks below), so it's a true two-way binding
  // rather than state that's merely seeded from the url once at mount.
  useUrlVersion();
  const tabParam = new URLSearchParams(location.search).get("tab");
  const setTab = (next: LaunchTab) => {
    const params = new URLSearchParams(location.search);
    // Always written explicitly (even "bookmarks"): the no-param default is
    // content-dependent below, so a clicked tab must pin itself in the URL.
    params.set("tab", next);
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };
  // Each tab folds at a flat MAX_CARDS and keeps its own "Show more" state,
  // so switching tabs doesn't reset (or leak) the other tab's expansion.
  const [expandedBookmarks, setExpandedBookmarks] = useState(false);
  const [expandedRecents, setExpandedRecents] = useState(false);
  const [expandedSessions, setExpandedSessions] = useState(false);
  const [expandedRepos, setExpandedRepos] = useState(false);
  // Folders flattened: the homepage is a launcher, and a bookmark buried two
  // folders deep is still one the user cared enough to save. Saved-list order.
  const bookmarks = loadBookmarks().length ? allBookmarks() : [];
  const shownBookmarks = expandedBookmarks ? bookmarks : bookmarks.slice(0, MAX_CARDS);
  // Raw MRU (newest first) — not the sidebar's stable-slot top-3, since a
  // full page doesn't jump under the pointer the way an always-visible
  // sidebar section does.
  const recents = loadRecents().entries;
  const shownRecents = expandedRecents ? recents : recents.slice(0, MAX_CARDS);
  // Claude session folders have no client-side cache like bookmarks/recents —
  // one cheap GET on mount, independent of which tab is showing, so switching
  // to the tab never shows a fetch-in-flight blip.
  const [sessionFolders, setSessionFolders] = useState<ClaudeSessionFolder[] | null>(null);
  useEffect(() => {
    let alive = true;
    getClaudeSessionFolders().then(
      (r) => alive && setSessionFolders(r.folders),
      () => alive && setSessionFolders([]),
    );
    return () => {
      alive = false;
    };
  }, []);
  const shownSessions =
    sessionFolders && (expandedSessions ? sessionFolders : sessionFolders.slice(0, MAX_CARDS));
  // Git repos, same deal as the session folders: one cheap GET on mount whatever
  // tab is showing. The whole response is kept, not just the list — the tab has
  // to tell "no repos on this machine" apart from "the first index scan hasn't
  // finished", and only `indexed`/`scanning` carry that. A failed request lands
  // on a null response, which the tab renders as its own message rather than as
  // an empty repo list.
  const [repos, setRepos] = useState<GitRepos | null>(null);
  const [reposFailed, setReposFailed] = useState(false);
  // Search takes over the page body (bookmarks/recents hide behind it) for as
  // long as there is a query — instant results appear while typing, so the
  // grids yield from the first keystroke rather than on a submit. Declared
  // here because the index poll below is shared with the search box.
  const [searching, setSearching] = useState(false);
  // ...but "one GET on mount" alone would strand the user, because the answer can
  // arrive LATER: this list is derived from the file index, and a homepage opened
  // while the first scan is running would sit on "Still building…" until something
  // remounted the page — which switching tabs does not do (the tab is a URL param,
  // not a route). That is not an edge case; it is every user's state right after an
  // upgrade that changes the index rules and forces a rescan.
  //
  // So the existing index poller drives the refetch (useIndexStatus, shared with
  // the listing's search indicator) rather than a poll of our own: it already
  // knows both scan rates.
  //
  // It runs until the answer is FINAL, not merely present (reposNeedsIndexPoll).
  // Gating on `!indexed` stopped the poll the instant a stale-but-served list
  // arrived — which froze `indexKey` below, so the cards and their "Reindexing"
  // note stayed on screen forever, even after the scan that would have cleared
  // them finished.
  //
  // `searching` widens the same poll rather than adding a second one: the
  // search box needs the scan state for its "indexing…" caveat, and the repos
  // gate goes quiet exactly when the index is healthy — which is when a scan
  // starting mid-session would otherwise go unnoticed by the box. One poller,
  // two consumers, the listing's rule (poll only while a search is open) still
  // honoured for the search half.
  const indexScan = useIndexStatus(reposNeedsIndexPoll(repos) || searching);
  // Refetch whenever the index's OBSERVABLE STATE changes — not when a scan
  // "completes". Completion was the previous trigger and it was subtly wrong:
  // `last_completed_at` is read off the manifest, and a cancelled, failed or
  // killed run stops without ever writing one (runner.derive_state sets running
  // false on any run_end; _with_liveness does the same for an abandoned worker).
  // So those runs moved `scanning` true -> false with `last_completed_at` frozen,
  // no refetch fired, and the tab sat on "Still building…" with nothing running.
  //
  // Keying on the pair covers every way a scan can end — completed, cancelled,
  // failed, killed — and also the start of one, with no transition bookkeeping to
  // get wrong. Extra refetches are harmless (the request is a cheap read and the
  // view is a pure function of its result), which is the point: this trigger is
  // deliberately over-eager rather than clever.
  const indexKey = indexScan
    ? `${indexScan.scanning}|${indexScan.last_completed_at ?? ""}`
    : null;
  // The key the response we are HOLDING was fetched under. `undefined` means
  // nothing has been fetched yet, which no real key can equal.
  const [fetchedKey, setFetchedKey] = useState<string | null | undefined>(undefined);
  useEffect(() => {
    let alive = true;
    const key = indexKey;
    getGitRepos().then(
      (r) => {
        if (!alive) return;
        setReposFailed(false);
        setRepos(r);
        setFetchedKey(key);
      },
      () => {
        if (!alive) return;
        setReposFailed(true);
        // Stamped on failure too, or `refreshPending` would stay true forever and
        // the tab would claim something is coming when nothing is.
        setFetchedKey(key);
      },
    );
    return () => {
      alive = false;
    };
  }, [indexKey]);
  // DERIVED, not stored in an effect. The instant `indexKey` changes, this is
  // already true in the very same render — which is the whole point: a
  // `setRefreshPending(true)` in an effect lands one frame late, and that one frame
  // is precisely the scan-end flicker (the tab showing "go rebuild it" between the
  // poll going idle and the new list arriving). Triggered is not arrived, so the
  // view has to be able to see the gap rather than be told about it afterwards.
  //
  // The rule itself (notably: the first poll reading is a BASELINE, not a change)
  // lives in refreshIsPending so it is covered by the state-table walk.
  const refreshPending = refreshIsPending(fetchedKey, indexKey);
  // One total function over all four inputs, so no impossible in-between state can
  // be rendered — see the state table in lib/repos.ts.
  const reposTab = reposView({
    response: repos,
    failed: reposFailed,
    liveScanning: indexScan?.scanning ?? null,
    refreshPending,
  });
  const repoList = reposTab.kind === "ready" ? reposTab.repos : [];
  const shownRepos = expandedRepos ? repoList : repoList.slice(0, MAX_CARDS);
  // With no ?tab= in the URL, land on Claude sessions — the leading tab.
  // Bookmark/recent caches still hydrate on mount (effect below) so the other
  // tabs are ready when clicked. An explicit ?tab= always wins.
  useEffect(() => {
    // Both are idempotent (enqueued; bookmarks no-ops when already hydrated).
    void hydrateBookmarks();
    void hydrateRecents();
  }, []);
  const tab: LaunchTab =
    tabParam === "recents" ||
    tabParam === "sessions" ||
    tabParam === "bookmarks" ||
    tabParam === "repos"
      ? tabParam
      : "sessions";
  // A ?q= present at load was a committed AI search; FilesSearch re-runs it.
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;

  return (
    <div className="files-home">
      <div className="files-home-inner">
        {/* No brand row or headline: the search prompt is the whole hero —
            the page title lives in the sidebar's Explorer entry, and a
            "Find and preview your files" restatement above the box only
            pushed the one thing you came to use further down. */}
        <header className="home-hero files-hero">
          <FilesSearch
            home={home}
            initialQuery={initialQuery}
            indexScan={indexScan}
            onActiveChange={setSearching}
          />
        </header>

        {searching ? null : (
          <>
          <section className="fh-section">
            <div className="fh-tabs">
              <div className="fh-tablist" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={tab === "sessions"}
                className={"fh-tab" + (tab === "sessions" ? " active" : "")}
                onClick={() => setTab("sessions")}
              >
                Artifacts
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "bookmarks"}
                className={"fh-tab" + (tab === "bookmarks" ? " active" : "")}
                onClick={() => setTab("bookmarks")}
              >
                Bookmarks
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "recents"}
                className={"fh-tab" + (tab === "recents" ? " active" : "")}
                onClick={() => setTab("recents")}
              >
                Recents
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={tab === "repos"}
                className={"fh-tab" + (tab === "repos" ? " active" : "")}
                onClick={() => setTab("repos")}
              >
                Repos
              </button>
              </div>
              {/* Browse rides the tab strip's right edge — the one action in a
                  row of filters, so it sits opposite them rather than above
                  them. Like the grids, it yields to search results. */}
              <button
                type="button"
                className="files-hero-cta"
                onClick={() => navigate(home, { isDir: true })}
              >
                Browse files
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </button>
            </div>

            {tab === "bookmarks" ? (
              bookmarks.length ? (
                <>
                  <div className="fhb-grid">
                    {shownBookmarks.map((b) => (
                      <BookmarkPreviewCard key={b.id} b={b} />
                    ))}
                  </div>
                  {bookmarks.length > MAX_CARDS && (
                    <ShowMoreButton
                      expanded={expandedBookmarks}
                      onClick={() => setExpandedBookmarks((v) => !v)}
                    />
                  )}
                </>
              ) : (
                <p className="fh-empty">
                  No bookmarks yet. While browsing, use the star in the breadcrumb to save a
                  spot — it'll show up here.
                </p>
              )
            ) : tab === "recents" ? (
              recents.length ? (
                <>
                  <div className="fhb-grid">
                    {shownRecents.map((r) => {
                      const fsPath = recentFsPath(r.url);
                      return (
                        <RecentPreviewCard
                          key={fsPath}
                          url={r.url}
                          path={fsPath}
                          name={r.title || basename(fsPath)}
                        />
                      );
                    })}
                  </div>
                  {recents.length > MAX_CARDS && (
                    <ShowMoreButton
                      expanded={expandedRecents}
                      onClick={() => setExpandedRecents((v) => !v)}
                    />
                  )}
                </>
              ) : (
                <p className="fh-empty">Nothing opened yet. Files you view will show up here.</p>
              )
            ) : tab === "repos" ? (
              repoList.length ? (
                <>
                  {/* A stale list still shows every card; this is a footnote, not a
                      warning. An index is always a little behind the filesystem,
                      so alarming copy here would cry wolf permanently. */}
                  {reposStaleNote(reposTab) && (
                    <p className="fh-search-summary">{reposStaleNote(reposTab)}</p>
                  )}
                  <div className="fhb-grid">
                    {shownRepos.map((r) => (
                      <FolderPreviewCard key={r.path} path={r.path} />
                    ))}
                  </div>
                  {repoList.length > MAX_CARDS && (
                    <ShowMoreButton
                      expanded={expandedRepos}
                      onClick={() => setExpandedRepos((v) => !v)}
                    />
                  )}
                </>
              ) : (
                // Every no-cards case routes through the same function, so "you
                // have no repos" can never be shown for "we haven't finished
                // looking" (or the reverse) — the distinction the index makes
                // necessary, and the one three earlier versions of this got wrong.
                <p className="fh-empty">{reposMessage(reposTab)}</p>
              )
            ) : shownSessions === null ? (
              <p className="fh-empty">Looking for artifacts…</p>
            ) : sessionFolders && sessionFolders.length ? (
              <>
                <div className="fhb-grid">
                  {shownSessions.map((f) => (
                    <FolderPreviewCard key={f.path} path={f.path} />
                  ))}
                </div>
                {sessionFolders.length > MAX_CARDS && (
                  <ShowMoreButton
                    expanded={expandedSessions}
                    onClick={() => setExpandedSessions((v) => !v)}
                  />
                )}
              </>
            ) : (
              <p className="fh-empty">
                No Claude Code sessions found on this machine.
              </p>
            )}
          </section>
          </>
        )}
      </div>
    </div>
  );
}
