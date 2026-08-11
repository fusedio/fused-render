// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useEffect, useMemo, useRef, useState } from "react";
import { navigate, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { basename, formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config, ClaudeSessionFolder } from "@platform/lib/api";
import { getClaudeSessionFolders, indexSearch, statPath } from "@platform/lib/api";
import { allBookmarks, hydrateBookmarks, loadBookmarks } from "@platform/lib/bookmarks";
import { useBookmarksVersion, useUrlVersion } from "@platform/lib/hooks";
import {
  fsMutationCount,
  indexLifecycleCount,
  subscribeFsMutations,
  subscribeIndexLifecycle,
} from "@platform/lib/index-freshness";
import { hydrateRecents, loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { BookmarkPreviewCard, RecentPreviewCard, ClaudeSessionFolderCard } from "@apps/explorer/BookmarkCards";
import { describeSpec, runAiSearch, type AiSearchResult } from "@apps/explorer/lib/ai-search";
import {
  INSTANT_DEBOUNCE_MS,
  activeRow,
  corpusFrom,
  homeCountNote,
  homeHitsFrom,
  isAiRow,
  pathShortcut,
  rankingSettled,
  redirectsToSearch,
  stepHighlight,
  submitRow,
  type CorpusState,
  type HomeHit,
} from "@apps/explorer/lib/home-search";
import { queryWantsHidden, rankCompare, scoreEntries } from "@apps/explorer/listing/search";
import { startScanJob } from "@apps/explorer/listing/scan-job";
import {
  RERANK_COMMIT_MS,
  SCAN_IMMEDIATE_MAX,
  SCAN_SLICE,
  type SearchHit,
} from "@apps/explorer/listing/types";
import { ErrorBanner } from "@platform/ui/ErrorBanner";

// How many cards the Bookmarks/Recents tab shows before "Show more" — flat
// count, not a row multiple, so it's the same rule for either tab regardless
// of how many columns the grid happens to lay out at the current width.
// 9 fills a 3×3 grid at the layout's usual three columns.
const MAX_CARDS = 9;

type LaunchTab = "bookmarks" | "recents" | "sessions";

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
  onActiveChange,
}: {
  home: string;
  initialQuery: string;
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
  const [corpus, setCorpus] = useState<CorpusState>({ status: "idle" });
  // A scan finishing or the index being deleted changes what the corpus IS,
  // and no other signal reports it (the filesystem did not change) — see
  // lib/index-freshness.
  const [lifecycle, setLifecycle] = useState(indexLifecycleCount);
  useEffect(() => subscribeIndexLifecycle(() => setLifecycle(indexLifecycleCount())), []);
  // An in-app rename/delete moves paths the fetched corpus already holds, so
  // search would find the old name and the click would 404. It is a REFETCH
  // trigger, not a gate: `indexMayAnswer(home)` used to disable instant search
  // outright, and since `touched` is session-scoped and home is an ancestor of
  // every mutation, one rename anywhere pinned this page to "still building"
  // for the rest of the session — while the index was in fact built. The home
  // page has no live walk to fall back on, so switching search OFF is the worst
  // available outcome: a corpus one rename stale beats no corpus at all.
  // (useWalkSearch keeps the gate because it HAS a live walk to prefer.)
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
    setCorpus((prev) => (prev.status === "ok" ? prev : { status: "loading" }));
    indexSearch(home, { signal: ctl.signal }).then(
      (res) => {
        if (!ctl.signal.aborted) setCorpus(corpusFrom(res));
      },
      (err: Error) => {
        if (ctl.signal.aborted || err.name === "AbortError") return;
        setCorpus({ status: "error", message: err.message });
      },
    );
    return () => ctl.abort();
  }, [home, wanted, lifecycle, mutations, retryNonce]);

  // -- ranking ---------------------------------------------------------------
  // The same sliced, cancellable scan the in-folder search runs: a covered home
  // root can be 200k entries, and scoring that synchronously on a keystroke is
  // the typing freeze listing/scan-job exists to prevent.
  const entries = corpus.status === "ok" ? corpus.entries : null;
  const [scanned, setScanned] = useState<{ q: string; items: SearchHit[]; done: boolean }>({
    q: "",
    items: [],
    done: true,
  });
  useEffect(() => {
    if (entries === null || q === "") {
      setScanned((prev) =>
        prev.q === q && prev.items.length === 0 && prev.done ? prev : { q, items: [], done: true },
      );
      return;
    }
    return startScanJob(
      {
        q,
        showHidden: queryWantsHidden(q),
        entries,
        from: 0,
        ranked: [],
        sliceSize: SCAN_SLICE,
        immediateMax: SCAN_IMMEDIATE_MAX,
        debounceMs: INSTANT_DEBOUNCE_MS,
        commitMs: RERANK_COMMIT_MS,
      },
      {
        score: scoreEntries,
        sort: (hitsToSort) => hitsToSort.sort(rankCompare),
        now: Date.now,
        setTimer: (fn, ms) => window.setTimeout(fn, ms),
        clearTimer: (id) => window.clearTimeout(id),
        onPublish: (result, done) => setScanned({ ...result, done }),
        onProgress: () => {},
      },
    );
  }, [q, entries]);

  // Rows are only ever shown under the query they were scored for.
  const ranked = scanned.q === q ? scanned.items : [];
  const hits = useMemo(() => homeHitsFrom(ranked, home), [ranked, home]);
  const scanning = active && (scanned.q !== q || !scanned.done);

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
  useEffect(() => () => aiCtl.current?.abort(), []);
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
    setQuery("");
    setAi(AI_OFF);
    setHighlight(null);
    syncQueryParam(null);
  };

  const edit = (value: string) => {
    setQuery(value);
    setHighlight(null);
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
  const settled = rankingSettled(corpus.status, scanning);
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
        try {
          const st = await statPath(target);
          navigate(st.path, { isDir: st.is_dir });
          return;
        } catch {
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
            {corpus.status === "cold" ? (
              // Never "no matches" for an index that has not been built: that
              // would blame the user's files for the app's state.
              "The file index is still building — AI search can answer in the meantime."
            ) : corpus.status === "error" ? (
              `The file index could not be searched: ${corpus.message}`
            ) : corpus.status !== "ok" || scanning ? (
              "Searching…"
            ) : ranked.length === 0 ? (
              `No file name matched “${q}” — AI search can look at dates, types and sizes.`
            ) : (
              <>
                {homeCountNote(ranked.length, corpus.truncated)}
                {" · "}
                <kbd>↑</kbd>
                <kbd>↓</kbd> to pick · <kbd>esc</kbd> to clear
              </>
            )}
          </p>
          <ul className="fh-results" id="fh-result-list" role="listbox" aria-label="Search results">
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
  // With no ?tab= in the URL, land on Claude sessions — the leading tab.
  // Bookmark/recent caches still hydrate on mount (effect below) so the other
  // tabs are ready when clicked. An explicit ?tab= always wins.
  useEffect(() => {
    // Both are idempotent (enqueued; bookmarks no-ops when already hydrated).
    void hydrateBookmarks();
    void hydrateRecents();
  }, []);
  const tab: LaunchTab =
    tabParam === "recents" || tabParam === "sessions" || tabParam === "bookmarks"
      ? tabParam
      : "sessions";
  // Search takes over the page body (bookmarks/recents hide behind it) for as
  // long as there is a query — instant results appear while typing, so the
  // grids yield from the first keystroke rather than on a submit.
  const [searching, setSearching] = useState(false);
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
          <FilesSearch home={home} initialQuery={initialQuery} onActiveChange={setSearching} />
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
            ) : shownSessions === null ? (
              <p className="fh-empty">Looking for artifacts…</p>
            ) : sessionFolders && sessionFolders.length ? (
              <>
                <div className="fhb-grid">
                  {shownSessions.map((f) => (
                    <ClaudeSessionFolderCard key={f.path} path={f.path} />
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
