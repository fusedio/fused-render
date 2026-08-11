// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useEffect, useRef, useState } from "react";
import { navigate, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { basename, formatMtime, formatMtimeFull, formatSize } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config, ClaudeSessionFolder } from "@platform/lib/api";
import { getClaudeSessionFolders, statPath } from "@platform/lib/api";
import { allBookmarks, hydrateBookmarks, loadBookmarks } from "@platform/lib/bookmarks";
import { useBookmarksVersion, useUrlVersion } from "@platform/lib/hooks";
import { hydrateRecents, loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { BookmarkPreviewCard, RecentPreviewCard, ClaudeSessionFolderCard } from "@apps/explorer/BookmarkCards";
import { describeSpec, runAiSearch, type AiSearchResult } from "@apps/explorer/lib/ai-search";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TextArea } from "@platform/ui/field/fields";

// How many cards the Bookmarks/Recents tab shows before "Show more" — flat
// count, not a row multiple, so it's the same rule for either tab regardless
// of how many columns the grid happens to lay out at the current width.
// 9 fills a 3×3 grid at the layout's usual three columns.
const MAX_CARDS = 9;

type LaunchTab = "bookmarks" | "recents" | "sessions";

// -- AI search (the files-home composer) --------------------------------------

type SearchPhase = "idle" | "searching";

// The hero's search box, styled after the /apps hero composer: one line of
// natural language in, one haiku call to interpret it, one indexed SQL query to
// answer it (see lib/ai-search.ts). While a result is showing, the homepage's
// bookmark/recent grids yield to the result grid; Clear (or emptying the box)
// brings them back.
function AiSearchComposer({
  home,
  initialQuery,
  onResult,
  onClear,
  active,
}: {
  home: string;
  initialQuery: string;
  onResult: (query: string, r: AiSearchResult) => void;
  onClear: () => void;
  active: boolean;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [phase, setPhase] = useState<SearchPhase>("idle");
  const [error, setError] = useState<string | null>(null);
  // One in-flight search at a time: a new submit aborts the previous walk.
  const abortRef = useRef<AbortController | null>(null);
  useEffect(() => () => abortRef.current?.abort(), []);

  const clear = () => {
    abortRef.current?.abort();
    setQuery("");
    setPhase("idle");
    setError(null);
    onClear();
  };

  const submit = async () => {
    const q = query.trim();
    if (!q || phase === "searching") return;
    abortRef.current?.abort();
    const ctl = new AbortController();
    abortRef.current = ctl;
    setError(null);
    setPhase("searching");
    try {
      // A pasted absolute path (/, ~/, or C:/) that actually exists opens
      // directly — no reason to run an AI search over an exact address. A
      // non-existent one falls through to the normal search.
      if (/^(\/|~\/|~$|[A-Za-z]:[\\/])/.test(q)) {
        let fsPath = q === "~" || q.startsWith("~/") ? home + q.slice(1) : q;
        // Backslashes are only separators in drive-letter paths (same rule
        // as the shell's path codec) — on POSIX "\" is a legal filename char.
        if (/^[A-Za-z]:[\\/]/.test(fsPath)) fsPath = fsPath.replace(/\\/g, "/");
        // Strip a trailing slash but keep roots whole: "/" stays "/", and a
        // drive root keeps its slash (bare "C:" reads as cwd-relative).
        fsPath = fsPath.replace(/\/+$/, "") || "/";
        if (/^[A-Za-z]:$/.test(fsPath)) fsPath += "/";
        try {
          const st = await statPath(fsPath);
          if (ctl.signal.aborted) return;
          navigate(st.path, { isDir: st.is_dir });
          return;
        } catch {
          if (ctl.signal.aborted) return;
          // not a real path — treat it as a search query
        }
      }
      const res = await runAiSearch(home, q, ctl.signal);
      if (ctl.signal.aborted) return;
      setPhase("idle");
      onResult(q, res);
    } catch (e) {
      if (ctl.signal.aborted) return;
      setPhase("idle");
      setError((e as Error).message);
    }
  };

  // A URL-restored query (?q=…) runs on mount: the URL is the state of
  // record (same ethos as the listing's ?q=), so landing on a search URL
  // must reproduce the search, not just prefill the box.
  const ranInitial = useRef(false);
  useEffect(() => {
    if (ranInitial.current) return;
    ranInitial.current = true;
    if (initialQuery.trim()) void submit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const busy = phase === "searching";
  return (
    <div className="home-composer-wrap files-search-wrap">
      <div className={"home-composer files-search" + (busy ? " is-busy" : "")}>
        {/* The clear ✕ floats at the input's right edge (not in the footer
            bar) so wiping the query reads as an input affordance. */}
        <div className="files-search-field">
          <TextArea
            className="home-composer-input"
            placeholder="Search your files — “big csv from last week” — or paste a path like ~/Downloads"
            aria-label="Search your files"
            value={query}
            rows={1}
            disabled={busy}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              // Enter submits (a search is a one-shot prompt); Shift+Enter
              // keeps the newline — same contract as the /apps composer.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape" && active) {
                e.preventDefault();
                clear();
              }
            }}
          />
          {(active || query !== "") && !busy && (
            <button
              type="button"
              className="files-search-clear"
              title="Clear search (esc)"
              onClick={clear}
            >
              Clear
            </button>
          )}
        </div>
        <div className="home-composer-bar">
          <span className="home-composer-hint">
            {busy ? (
              "Searching…"
            ) : (
              <>
                <kbd>↵</kbd> to search · <kbd>⇧↵</kbd> for a new line
                {active && (
                  <>
                    {" · "}
                    <kbd>esc</kbd> to clear
                  </>
                )}
              </>
            )}
          </span>
          <button
            type="button"
            className="home-composer-send"
            aria-label="Search"
            title="Search"
            disabled={!query.trim() || busy}
            onClick={submit}
          >
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
          </button>
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
    </div>
  );
}

// Result list: a flat scannable list (relevance order), not the launcher card
// grid, so hits get room for path and dates.
function SearchResults({
  home,
  query,
  result,
}: {
  home: string;
  query: string;
  result: AiSearchResult;
}) {
  const summary = describeSpec(result.spec);
  return (
    <section className="fh-section">
      <h2 className="fh-heading">Results for “{query}”</h2>
      <p className="fh-search-summary">
        {result.usedFallback
          ? "AI unavailable — matched your words directly."
          : summary
            ? `Understood as: ${summary}`
            : "No filters — showing closest matches."}
        {result.truncated && " · Broad query: showing the first slice of matches."}
      </p>
      {result.hits.length ? (
        // A flat list, not the launcher card grid: results are scanned
        // top-to-bottom by relevance, and a row gives the path and dates
        // room the cards don't have.
        <ul className="fh-results">
          {result.hits.map((h) => {
            const display = h.path.startsWith(home + "/")
              ? "~/" + h.path.slice(home.length + 1)
              : h.path;
            return (
              <li key={h.path}>
                <a
                  className="fh-result"
                  href={urlForFsPath(h.path)}
                  title={h.path}
                  onClick={(e) => {
                    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
                      return;
                    e.preventDefault();
                    navigate(h.path, { isDir: h.is_dir });
                  }}
                >
                  <span className="fh-result-icon" aria-hidden="true">
                    {iconForEntry(basename(h.path), h.is_dir)}
                  </span>
                  <span className="fh-result-name">{basename(h.path)}</span>
                  <span className="fh-result-path">{display}</span>
                  <span className="fh-result-meta">
                    {h.is_dir ? "" : formatSize(h.size)}
                    {h.mtime !== null && (
                      <span className="fh-result-date" title={formatMtimeFull(h.mtime)}>
                        {formatMtime(h.mtime)}
                      </span>
                    )}
                  </span>
                </a>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="fh-empty">Nothing matched. Try different words, or fewer of them.</p>
      )}
    </section>
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
  // A committed AI search result takes over the page body (bookmarks/recents
  // hide behind it) until cleared — the homepage becomes the result page.
  const [search, setSearch] = useState<{ query: string; result: AiSearchResult } | null>(null);
  // The committed query rides the URL (?q=…, same ethos as the listing
  // search): submit writes it, Clear removes it, and a load with ?q= present
  // re-runs the search via the composer's initialQuery.
  const initialQuery = useRef(new URLSearchParams(location.search).get("q") || "").current;
  const syncQueryParam = (q: string | null) => {
    const params = new URLSearchParams(location.search);
    if (q) params.set("q", q);
    else params.delete("q");
    const qs = params.toString();
    replaceSearch(location.pathname + (qs ? "?" + qs : ""));
  };

  return (
    <div className="files-home">
      <div className="files-home-inner">
        {/* No brand row or headline: the search prompt is the whole hero —
            the page title lives in the sidebar's Explorer entry, and a
            "Find and preview your files" restatement above the box only
            pushed the one thing you came to use further down. */}
        <header className="home-hero files-hero">
          <AiSearchComposer
            home={home}
            initialQuery={initialQuery}
            active={search !== null}
            onResult={(query, result) => {
              setSearch({ query, result });
              syncQueryParam(query);
            }}
            onClear={() => {
              setSearch(null);
              syncQueryParam(null);
            }}
          />
        </header>

        {search ? (
          <SearchResults home={home} query={search.query} result={search.result} />
        ) : (
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
