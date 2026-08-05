// The file explorer's homepage (/explorer): a hero that says what the
// explorer is for (same visual ethos as the /apps hero — brand row, headline,
// one accent moment) over card grids for the two things worth jumping to:
// bookmarks and recent files. Entering any target navigates into
// /explorer/view/... (the explorer proper).
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { navigate, navigateUrl, replaceSearch, urlForFsPath } from "@platform/lib/router";
import { basename } from "@platform/lib/format";
import { iconForEntry } from "@platform/ui/FileIcons";
import type { Config } from "@platform/lib/api";
import { allBookmarks, loadBookmarks } from "@platform/lib/bookmarks";
import { useBookmarksVersion } from "@platform/lib/hooks";
import { loadRecents, recentFsPath, useRecentsVersion } from "@apps/explorer/lib/recents";
import { BookmarkPreviewCard } from "@apps/explorer/BookmarkCards";
import { describeSpec, runAiSearch, type AiSearchResult } from "@apps/explorer/lib/ai-search";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { TextArea } from "@platform/ui/field/fields";
import logoMark from "@assets/logo-black-bg-transparent.png";

// How many recent files earn a card. The sidebar shows a tight top-3; the
// homepage has room to be a real jump-off point.
const MAX_RECENT_CARDS = 8;

// How many bookmark-grid rows show before the "Show more" fold.
const BOOKMARK_ROWS = 2;

// The bookmark grid's live column count, so the two-row fold shows exactly
// two full rows at any viewport width. Measured from the grid's resolved
// template (auto-fill decides the count) and re-measured on resize.
function useGridColumns(ref: React.RefObject<HTMLDivElement | null>, mounted: boolean): number {
  const [cols, setCols] = useState(1);
  // `mounted` (the grid renders only when there are bookmarks) re-runs the
  // effect when the grid appears — the ref object itself never changes.
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () =>
      setCols(getComputedStyle(el).gridTemplateColumns.split(" ").length || 1);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref, mounted]);
  return cols;
}

// A launcher card: icon tile + name + path. Shared shape for bookmarks,
// recents, and the workspace root so the grids read as one system. An anchor
// so middle-click / Cmd-click open a new tab (same rationale as app cards).
function LaunchCard({
  href,
  icon,
  name,
  path,
  onOpen,
}: {
  href: string;
  icon: ReactNode;
  name: string;
  path: string;
  onOpen: () => void;
}) {
  return (
    <a
      className="fh-card"
      href={href}
      title={path}
      onClick={(e) => {
        if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
          return;
        e.preventDefault();
        onOpen();
      }}
    >
      <span className="fh-card-icon" aria-hidden="true">
        {icon}
      </span>
      <span className="fh-card-text">
        <span className="fh-card-name">{name}</span>
        <span className="fh-card-path">{path}</span>
      </span>
    </a>
  );
}

// -- AI search (the files-home composer) --------------------------------------

type SearchPhase = "idle" | "searching";

// The hero's search box, styled after the /apps hero composer: one line of
// natural language in, one haiku call to interpret it, one bounded walk to
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
            placeholder="Search your files — “big csv from last week”, “notebook about weather”…"
            aria-label="Search your files"
            value={query}
            rows={3}
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
              aria-label="Clear search"
              title="Clear search"
              onClick={clear}
            >
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
                <path d="M6 6l12 12M18 6L6 18" />
              </svg>
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

// Result grid: the same LaunchCard shape as recents, so hits read as one
// system with the rest of the homepage.
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
        {result.engine === "walk" && " · Searched your home folder."}
        {result.truncated && " · Broad query: showing the first slice of matches."}
      </p>
      {result.hits.length ? (
        <div className="fh-grid">
          {result.hits.map((h) => {
            const display = h.path.startsWith(home + "/")
              ? "~/" + h.path.slice(home.length + 1)
              : h.path;
            return (
              <LaunchCard
                key={h.path}
                href={urlForFsPath(h.path)}
                icon={iconForEntry(basename(h.path), h.is_dir)}
                name={basename(h.path)}
                path={display}
                onOpen={() => navigate(h.path, { isDir: h.is_dir })}
              />
            );
          })}
        </div>
      ) : (
        <p className="fh-empty">Nothing matched. Try different words, or fewer of them.</p>
      )}
    </section>
  );
}

export default function FilesHome({ config }: { config: Config }) {
  useBookmarksVersion();
  useRecentsVersion();
  // Folders flattened: the homepage is a launcher, and a bookmark buried two
  // folders deep is still one the user cared enough to save.
  const bookmarks = loadBookmarks().length ? allBookmarks() : [];
  // Bookmarks fold at two grid rows; "Show more" renders the rest in place
  // (recents just move down) and flips to "Show less" to collapse again.
  const [expanded, setExpanded] = useState(false);
  const gridRef = useRef<HTMLDivElement | null>(null);
  const cols = useGridColumns(gridRef, bookmarks.length > 0);
  const fold = cols * BOOKMARK_ROWS;
  const shownBookmarks = expanded ? bookmarks : bookmarks.slice(0, fold);
  // Raw MRU, not the sidebar's stable-slot top-3 — a full page doesn't jump
  // under the pointer the way a always-visible sidebar section does.
  const recents = loadRecents().entries.slice(0, MAX_RECENT_CARDS);
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
        {/* Same shape as the /apps HomeHero: brand row, headline, composer —
            the hero's only verb is the search prompt, mirroring how /apps
            leads with its build prompt. */}
        <header className="home-hero files-hero">
          <div className="home-hero-brand">
            <img className="home-hero-logo" src={logoMark} alt="" aria-hidden="true" />
            <span className="home-hero-brand-name">Fused Explorer</span>
          </div>
          <h1 className="home-hero-title">
            Find and preview <span className="home-hero-accent">your files</span>
          </h1>
          <AiSearchComposer
            home={config.home}
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
          <SearchResults home={config.home} query={search.query} result={search.result} />
        ) : (
          <>
          {/* Browse lives with the content, not the hero (the hero is the
              search prompt now) — and like the grids it yields to results. */}
          {config.fused_dir && (
            <section className="fh-section files-browse">
              <p className="files-browse-sub">
                Open anything in your workspace — data, maps, images, notebooks. Preview
                it instantly, split views side by side, and bookmark the places you keep
                coming back to.
              </p>
              <button
                type="button"
                className="files-hero-cta"
                onClick={() => navigate(config.fused_dir!, { isDir: true })}
              >
                Browse workspace
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M5 12h14M13 6l6 6-6 6" />
                </svg>
              </button>
            </section>
          )}
          <section className="fh-section">
            <h2 className="fh-heading">Bookmarks</h2>
            {bookmarks.length ? (
              <>
                <div className="fhb-grid" ref={gridRef}>
                  {shownBookmarks.map((b) => (
                    <BookmarkPreviewCard key={b.id} b={b} />
                  ))}
                </div>
                {bookmarks.length > fold && (
                  <button type="button" className="fhb-more" onClick={() => setExpanded((v) => !v)}>
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
                )}
              </>
            ) : (
              <p className="fh-empty">
                No bookmarks yet. While browsing, use the star in the breadcrumb to save a
                spot — it'll show up here.
              </p>
            )}
          </section>

          <section className="fh-section">
            <h2 className="fh-heading">Recent files</h2>
            {recents.length ? (
              <div className="fh-grid">
                {recents.map((r) => {
                  const fsPath = recentFsPath(r.url);
                  const name = r.title || basename(fsPath);
                  return (
                    <LaunchCard
                      key={fsPath}
                      href={r.url}
                      icon={iconForEntry(basename(fsPath), false)}
                      name={name}
                      path={fsPath}
                      onOpen={() => navigateUrl(r.url)}
                    />
                  );
                })}
              </div>
            ) : (
              <p className="fh-empty">Nothing opened yet. Files you view will show up here.</p>
            )}
          </section>
          </>
        )}
      </div>
    </div>
  );
}
