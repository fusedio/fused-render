// Apps hub — lives at "/apps", chrome-free like Home (no sidebar, no
// breadcrumb). Every detected app in the workspace (GET /api/apps, one PAGE
// at a time — see PAGE_SIZE) as a grid of big preview cards — each thumbnail is the app itself rendered in a
// scaled, non-interactive iframe (AppPreviewCard). The list is narrowed by a
// filter row — a Category/Folders mode selector with chips derived from the
// apps themselves (categories from each folder's metadata.json, ordered
// curated-first then locale-alphabetical by app-categories; folders from the
// top-level tag dirs, plain code-unit sort) — and a search box
// (name/title/tag/category, case-insensitive); the selector sits at the row's
// left edge with the chips and search gathered at the right.
// Order is always recently-opened (modified time stands in
// for an app never opened); filtering never reorders cards relative to each
// other. Filter, search, order and the chip rows are all the SERVER's
// (routers/apps.py `_paged_apps`): the grid appends pages in the order they
// arrive and never re-sorts, because re-sorting would interleave page 2 into
// page 1 under the reader.
import { useEffect, useMemo, useRef, useState } from "react";
import { getAppsPage, getBackgroundAppsRunning } from "@platform/lib/api";
import type { AppInfo, AppsPage, Config } from "@platform/lib/api";
import { useCurrentAppsChanged } from "@platform/lib/tasksChanged";
import { appCardMenu } from "@platform/lib/appCardMenu";
import { runCommunity } from "@platform/lib/community";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import { orderCategories } from "@apps/builder/app-categories";
import { useNavEpoch } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { HomeHero } from "./HomeHero";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";
import { FdaStrip } from "@platform/ui/FdaStrip";

// How many cards one page asks for. The server fetches its catalog from a
// snapshot and hydrates only this many, and the grid grows by this many as the
// reader nears the bottom (the sentinel below) or asks for more. 24 is six
// rows at the layout's usual four columns: well past a tall viewport, so the
// first page never looks like a fold. Every card is a DOM subtree, a
// preview.png request and two IntersectionObserver entries — AppPreviewCard's
// near-viewport gate only spares the iframe — so what is NOT on a page is
// what this number saves.
const PAGE_SIZE = 24;

// The last first-page answer per filter, kept at MODULE scope so it outlives
// the page's unmount. Revisiting /apps is a common move (open an app, come
// back) and a grid drawn instantly from the previous answer, then quietly
// replaced, beats a skeleton every time. Stale for as long as one fetch
// takes: a card for an app deleted since is clickable and 404s on open, the
// same as one deleted while the page sat open.
const firstPages = new Map<string, AppsPage>();

// Which facet the chips filter by. "category" reads each app's authored
// metadata.json category; "repo" is the top-level workspace folder (tag):
// all / examples / local / showcase in a stock workspace.
type FilterMode = "category" | "repo";

// The `key`s are internal (mode is local state derived from which URL param is
// set, never a param itself), so the labels are free to say what a user calls
// the thing: the "repo" facet's chips are the top-level workspace FOLDERS apps
// were scanned out of, which is what a reader of the chip row sees.
const MODES: { key: FilterMode; label: string }[] = [
  { key: "category", label: "Category" },
  { key: "repo", label: "Folders" },
];

const modeLabel = (m: FilterMode): string =>
  MODES.find((x) => x.key === m)?.label ?? m;

type ShowcaseCatalog = { status?: string };

// Read the showcase catalog once per mount, purely to force the clone when
// it's missing.
//
// `catalog` first — a cheap local read (a folder scan), no lock, no network.
// Only when it reports no-cache (the clone is missing or the startup clone
// is still running) does this escalate to `refresh`: that call parks on the
// cache lock behind an in-flight startup clone (or performs the clone itself
// after a failed start), and its completion is the signal that
// <workspace>/showcase just landed — `onSynced` then refetches the grid so
// the first visit doesn't keep a stale listing until reload. An
// already-cloned catalog never touches the network here (it's cloned once,
// then left alone), so this never blocks on git after the first visit.
//
// Returns the clone's failure message when it refused (no usable git on the
// machine, no network, a foreign folder already at <workspace>/showcase).
// This used to be swallowed, which left the hub silently short of every
// showcase app with nothing on screen to say why; the local grid still loads
// either way, so it renders as a muted line rather than the page's error
// banner. The backend composes the whole user-facing sentence.
function useShowcaseSync(onSynced: () => void): string | null {
  const [syncError, setSyncError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    (async () => {
      const local = await runCommunity<ShowcaseCatalog>({ action: "catalog" });
      if (!alive) return;
      if (local.status !== "no-cache") return;
      await runCommunity<ShowcaseCatalog>({ action: "refresh" });
      if (!alive) return;
      onSynced();
    })().catch((e: Error) => {
      if (alive) setSyncError(e.message);
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per mount
  }, []);
  return syncError;
}

// Folder paths (keys of GET /api/apps/background/running) whose background
// daemon is currently live — feeds the "running" badge on background-app
// cards. Same decoration-only posture as useShowcaseSync above: one fetch per
// mount, no polling (a stale badge just means "reload to see it flip"), and a
// failure yields an empty set rather than a card-breaking error — failures
// just mean no badges.
function useRunningBackgroundApps(): Set<string> {
  const [running, setRunning] = useState<Set<string>>(new Set());
  useEffect(() => {
    let alive = true;
    getBackgroundAppsRunning()
      .then(({ running: byPath }) => {
        if (!alive) return;
        setRunning(new Set(Object.keys(byPath).filter((path) => byPath[path])));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, []);
  return running;
}

export default function Apps({ config }: { config: Config }) {
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  // The selected filter lives in the URL (`?category=` or `?tag=`), not in
  // state — the AiModels pattern: it makes the filter bookmarkable, survives a
  // reload, and puts the choice on the back button. useNavEpoch counts
  // pushState and popstate alike, so back/forward re-reads the URL. Default
  // (All) is the ABSENCE of both params, keeping /apps the clean URL for the
  // page. The two params are mutually exclusive — the chips are one selector,
  // so picking in one facet clears the other.
  const navEpoch = useNavEpoch();
  const { tag, category } = useMemo(() => {
    const params = new URLSearchParams(location.search);
    return { tag: params.get("tag"), category: params.get("category") };
  }, [navEpoch]);
  const setFilter = (facet: FilterMode, next: string | null) => {
    const params = new URLSearchParams(location.search);
    params.delete("tag");
    params.delete("category");
    if (next !== null) params.set(facet === "repo" ? "tag" : "category", next);
    const search = params.toString();
    // No-op only when the WHOLE search is unchanged — comparing just this
    // facet's value would make "All" a dead click while the other facet still
    // has a (hidden) selection to clear.
    if (search === new URLSearchParams(location.search).toString()) return;
    // navigateUrl (pushState), not replaceSearch: each chip selection is a
    // history entry so back/forward walks the filter history.
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };
  // Which chip set is showing. State (not derived) so flipping the selector
  // with nothing chosen sticks; the effect below re-derives it from the URL so
  // back/forward restores the facet a bookmarked filter belongs to.
  const [mode, setMode] = useState<FilterMode>(tag !== null ? "repo" : "category");
  useEffect(() => {
    if (tag !== null) setMode("repo");
    else if (category !== null) setMode("category");
  }, [tag, category]);
  // Bumped when the panel creates an app, and on the desk-changed announcement
  // (an icon picked from the sidebar's Projects row while the grid is on
  // screen): refetches the grid without clearing it, so the card's mark
  // follows the new icon.svg.
  const [nonce, setNonce] = useState(0);
  useCurrentAppsChanged(() => setNonce((n) => n + 1));
  // One context-menu portal for the whole grid, at the cursor coords — same
  // shape as the explorer listing's (Listing.tsx openRowMenu).
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);

  const openCardMenu = (e: React.MouseEvent, app: AppInfo) => {
    // The card is an anchor: without this the browser's own link menu wins.
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items: appCardMenu(app) });
  };

  // The search box, debounced into the request. Search is a server round trip
  // now (it has to be: a page of a filtered list cannot be cut on the client
  // from a list it does not hold), so a keystroke waits 150 ms for the next
  // one before it becomes a request. Against the server's snapshot a filtered
  // page is a few milliseconds plus hydrating one page of cards.
  const [q, setQ] = useState("");
  useEffect(() => {
    const t = setTimeout(() => setQ(query.trim()), 150);
    return () => clearTimeout(t);
  }, [query]);

  // The filter this grid is for. A new chip or query is a new list and its
  // first page is the right place to land, so the pages below are keyed by
  // it and discarded the render it changes — DURING render (React re-renders
  // before committing), not in an effect after paint: an effect would commit
  // one frame of the OLD list's pages under the new filter first, which for a
  // reader who had scrolled a hundred cards deep is a hundred mounts, image
  // requests and observers for cards about to vanish. `nonce` is deliberately
  // not in the key: a refetch after create/sync keeps the reader's place.
  const filterKey = `${tag ?? ""}\u0000${category ?? ""}\u0000${q}`;
  const [loaded, setLoaded] = useState<{ key: string; page: AppsPage | null }>(() => ({
    key: filterKey,
    page: firstPages.get(filterKey) ?? null,
  }));
  if (loaded.key !== filterKey) {
    setLoaded({ key: filterKey, page: firstPages.get(filterKey) ?? null });
  }
  const page = loaded.key === filterKey ? loaded.page : (firstPages.get(filterKey) ?? null);

  // Page 1 (and the refetch). One request per filter key / nonce, the previous
  // one aborted: a fast typist's intermediate queries never land out of order
  // over the one they meant. A `nonce` refetch asks for as many cards as are
  // already on screen (never fewer than a page) with `fresh`, so the snapshot
  // behind it is rebuilt and the reader keeps their place; a filter change
  // asks for the first page. A failed fetch keeps whatever grid is drawn —
  // the error is its own state, not a phase that blanks the cards.
  useEffect(() => {
    const ctl = new AbortController();
    const refetch = nonce > 0;
    const limit = refetch
      ? Math.max(PAGE_SIZE, firstPages.get(filterKey)?.apps.length ?? 0)
      : PAGE_SIZE;
    getAppsPage({ offset: 0, limit, tag, category, q, fresh: refetch }, ctl.signal).then(
      (res) => {
        if (ctl.signal.aborted) return;
        firstPages.set(filterKey, res);
        setError(null);
        setLoaded({ key: filterKey, page: res });
      },
      (e: Error) => {
        if (!ctl.signal.aborted) setError(e.message);
      },
    );
    return () => ctl.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- tag/category/q are inside filterKey
  }, [filterKey, nonce]);

  // The next page, appended. Guarded by `loadingMore` (one in flight) and by
  // there being more: the sentinel below asks for this on every intersect and
  // a tall viewport asks several times in a row. The appended result is also
  // stored as the filter's first-page answer so a revisit paints the whole
  // grown grid, not just its first page.
  const [loadingMore, setLoadingMore] = useState(false);
  const hasMore = page !== null && page.apps.length < page.total;
  const loadMore = () => {
    if (!page || !hasMore || loadingMore) return;
    setLoadingMore(true);
    const key = filterKey;
    getAppsPage({ offset: page.apps.length, limit: PAGE_SIZE, tag, category, q }).then(
      (res) => {
        setLoadingMore(false);
        const cur = firstPages.get(key);
        if (!cur || cur.apps.length !== res.offset) return; // a refetch moved under us
        const grown = { ...res, offset: 0, apps: [...cur.apps, ...res.apps] };
        firstPages.set(key, grown);
        setLoaded((l) => (l.key === key ? { key, page: grown } : l));
      },
      (e: Error) => {
        setLoadingMore(false);
        setError(e.message);
      },
    );
  };
  // Auto-extend: a sentinel after the grid, observed inside the page's own
  // scroller (the same root useNearViewport uses, since this page owns its
  // vertical scroll), with a generous lookahead so the next page is requested
  // before the reader reaches the last row.
  //
  // Re-observed on every page length, not just on `hasMore`. An observer
  // reports CHANGES of intersection, and after one page on a wide or tall
  // viewport the sentinel can still sit inside the lookahead zone — eight
  // columns put a whole page in three rows — so nothing would fire again and
  // the scroll would stall with the pill as the only way on. A fresh
  // `observe()` always delivers its initial state, which is what keeps this
  // going until the sentinel is pushed clear or `hasMore` flips.
  const sentinelRef = useRef<HTMLDivElement>(null);
  const shownCount = page?.apps.length ?? 0;
  useEffect(() => {
    const el = sentinelRef.current;
    if (!hasMore || !el) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadMore();
      },
      { root: el.closest(".apps-page"), rootMargin: "600px 0px" },
    );
    io.observe(el);
    return () => io.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- loadMore reads current state
  }, [hasMore, shownCount, loadingMore]);

  const showcaseError = useShowcaseSync(() => setNonce((n) => n + 1));
  const runningPaths = useRunningBackgroundApps();
  // Showcase apps are ordinary workspace apps now: the server clones the
  // community repo into <workspace>/showcase in the background on startup,
  // and the workspace scan picks it up like any other tag dir. No synthetic
  // chip, no separate catalog surface.
  //
  // Chips speak for the WHOLE catalog, which is why the server sends them
  // with every page rather than the client deriving them from the cards it
  // holds. Folders: the server's sorted tag list (minus exported `.fused`
  // rows — see app-categories repoChips for why). Categories: orderCategories
  // puts the curated running order (starters, local-ai, productivity,
  // geospatial) first, then locale-alphabetical for anything else a workspace
  // turns up. Card order in the grid is unaffected.
  const tags = page?.tags ?? [];
  const categories = orderCategories(page?.categories ?? []);
  const chips = mode === "repo" ? tags : categories;
  const active = mode === "repo" ? tag : category;

  return (
    <div className="apps-page">
      <div className="apps-inner">
        {/* Same hero as Home: prompt composer that names, scaffolds, and lands
            in the new app's claude chat. Creating from here refreshes the grid. */}
        <HomeHero onCreated={() => setNonce((n) => n + 1)} />
        {/* Shows ONLY when the Claude CLI isn't on PATH, with a one-click fix. */}
        <ClaudeHealthStrip />
        <FdaStrip />

        <div className="apps-toolbar">
          {/* Facet selector: which chip set filters the grid. Switching facets
              resets the filter to All — a selection from the old facet would
              otherwise keep narrowing the grid invisibly under the new chips. */}
          <div className="apps-filter-mode" role="group" aria-label="Filter by">
            {MODES.map((m) => (
              <button
                key={m.key}
                type="button"
                className={"apps-filter-mode-btn" + (mode === m.key ? " is-active" : "")}
                onClick={() => {
                  setMode(m.key);
                  setFilter(m.key, null);
                }}
              >
                {m.label}
              </button>
            ))}
          </div>
          {(chips.length > 0 || tag !== null || category !== null) && (
            <div className="apps-tags" role="group" aria-label={`Filter by ${modeLabel(mode)}`}>
              {/* Active only when nothing filters; clicking clears both params. */}
              <button
                type="button"
                className={"apps-tag-chip" + (tag === null && category === null ? " is-active" : "")}
                onClick={() => setFilter(mode, null)}
              >
                All
              </button>
              {chips.map((c) => (
                <button
                  key={c}
                  type="button"
                  className={"apps-tag-chip" + (active === c ? " is-active" : "")}
                  onClick={() => setFilter(mode, active === c ? null : c)}
                >
                  {c}
                </button>
              ))}
            </div>
          )}
          <div className="apps-search">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <circle cx="11" cy="11" r="7" />
              <path d="M21 21l-4.3-4.3" />
            </svg>
            <input
              type="search"
              placeholder="Search apps…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              autoFocus
            />
          </div>
        </div>

        {error && <ErrorBanner>{error}</ErrorBanner>}
        {/* Above the grid, not inside its empty state: the local apps below
            are fine, it is only the showcase half that is missing. */}
        {showcaseError && <div className="apps-showcase-note">{showcaseError}</div>}
        {/* Skeleton only while there is nothing drawable at all: once a page
            has landed the cards themselves are the loading indicator. */}
        {page === null && !error && <SkeletonLines rows={4} label="Loading apps" />}
        {page !== null && (
          <>
            {/* The FILTER count, never the page: "24 of 300" would read as a
                filter the reader did not set. */}
            <div className="apps-count">
              {page.total === page.total_all
                ? `${page.total_all} app${page.total_all === 1 ? "" : "s"}`
                : `${page.total} of ${page.total_all} apps`}
            </div>
            {page.apps.length === 0 ? (
              <div className="home-empty">
                {page.total_all === 0
                  ? "No apps yet. Describe one in the composer above to create it."
                  : "No apps match — clear the search or filter."}
              </div>
            ) : (
              <>
                <div className="apps-cards">
                  {page.apps.map((app) => (
                    <AppPreviewCard
                      key={app.path}
                      app={app}
                      onContextMenu={openCardMenu}
                      badge={runningPaths.has(app.path) ? "running" : undefined}
                    />
                  ))}
                </div>
                {hasMore && (
                  <>
                    <div ref={sentinelRef} className="apps-more-sentinel" aria-hidden="true" />
                    {/* The explorer home's fold pill (`.fhb-more`, preferences.css),
                        for the reader who scrolls with the keyboard or whose
                        observer never fires. The count is what is LEFT, so it
                        reads as progress rather than as a filter. */}
                    <button type="button" className="fhb-more" onClick={loadMore} disabled={loadingMore}>
                      {loadingMore
                        ? "Loading…"
                        : `Show more (${page.total - page.apps.length} remaining)`}
                    </button>
                  </>
                )}
              </>
            )}
          </>
        )}
      </div>

      {menu && (
        <ContextMenu x={menu.x} y={menu.y} items={menu.items} onClose={() => setMenu(null)} />
      )}
    </div>
  );
}
