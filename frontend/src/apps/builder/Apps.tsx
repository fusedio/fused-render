// Apps hub — lives at "/apps", chrome-free like Home (no sidebar, no
// breadcrumb). Every detected app in the workspace (GET /api/apps) as a grid
// of big preview cards — each thumbnail is the app itself rendered in a
// scaled, non-interactive iframe (AppPreviewCard). The list is narrowed by a
// filter row — a Category/Folders mode selector with chips derived from the
// apps themselves (categories from each folder's metadata.json, ordered
// learn-first then locale-alphabetical by app-categories; folders from the
// top-level tag dirs, plain code-unit sort) — and a search box
// (name/title/tag/category, case-insensitive); the selector sits at the row's
// left edge with the chips and search gathered at the right.
// Order is always recently-opened (modified time stands in
// for an app never opened — appEntry.sortApps); filtering never reorders cards
// relative to each other.
import { useEffect, useMemo, useState } from "react";
import { getApps, getHomeApps } from "@platform/lib/api";
import type { AppInfo, Config } from "@platform/lib/api";
import { appCardMenu } from "@platform/lib/appCardMenu";
import { sortApps } from "@platform/lib/appEntry";
import { runCommunity, SHOWCASE_TAG } from "@platform/lib/community";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppPreviewCard } from "@apps/builder/AppPreviewCard";
import { orderCategories, repoChips } from "@apps/builder/app-categories";
import { useNavEpoch } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { HomeHero } from "./HomeHero";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { ClaudeHealthStrip } from "@platform/ui/ClaudeHealthStrip";

// The grid's three phases. "partial" is a REAL, OPENABLE prefix of the final
// grid — Home's recent-first row (see the fetch effect) — not a placeholder:
// sortApps is recency-first, so the cards it holds are the ones the exhaustive
// catalog will also rank first, and the swap to "ok" appends rather than
// reshuffles. Errors ride alongside in their own state rather than as a fourth
// phase: a failed catalog fetch must not throw away a partial grid the user
// can already click.
type Loaded<T> =
  | { status: "loading" }
  | { status: "partial"; data: T }
  | { status: "ok"; data: T };

// How many cards the fast row asks for. The server caps it at HOME_APPS_LIMIT
// (12) and its fast path only skips the exhaustive walk when the recents FILL
// the request, so asking for more than a hub's first rows would buy nothing and
// cost the walk twice — see /api/apps/home.
const FAST_ROW = 12;

// The last exhaustive catalog this tab fetched, kept at MODULE scope so it
// outlives the page's unmount. Revisiting /apps is a common move (open an app,
// come back) and a full grid drawn instantly from the previous answer, then
// quietly replaced, beats a skeleton every time. Stale for as long as one
// fetch takes: a card for an app deleted since is clickable and 404s on open,
// the same as one deleted while the page sat open.
let catalogCache: AppInfo[] | null = null;

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

type ShowcaseCatalog = { status?: string; apps?: { slug: string; installed?: boolean }[] };

const clonedSet = (c: ShowcaseCatalog) =>
  new Set((c.apps ?? []).filter((a) => a.installed).map((a) => a.slug));

// Read the showcase install records once per mount; feeds the "cloned"
// badges on showcase cards.
//
// `catalog` first — a cheap local read (installs.json + folder scan), no lock,
// no network, so badges never wait on git. Only when it reports no-cache
// (the clone is missing or the startup clone is still running) does this
// escalate to `refresh`: that call parks on the cache lock behind an
// in-flight startup clone (or performs the clone itself after a failed
// start), and its completion is the signal that <workspace>/showcase just
// landed — `onSynced` then refetches the grid so the first visit doesn't
// keep a stale listing until reload. An already-cloned catalog never
// touches the network here (server start owns the fetch+ff sync), so a
// Clone click right after mount isn't stuck behind a fetch holding the
// lock. Decoration plus refetch only — failures just mean no badges.
function useShowcaseSync(onSynced: () => void): Set<string> {
  const [slugs, setSlugs] = useState<Set<string>>(new Set());
  useEffect(() => {
    let alive = true;
    (async () => {
      const local = await runCommunity<ShowcaseCatalog>({ action: "catalog" });
      if (!alive) return;
      if (local.status !== "no-cache") {
        setSlugs(clonedSet(local));
        return;
      }
      const synced = await runCommunity<ShowcaseCatalog>({ action: "refresh" });
      if (!alive) return;
      setSlugs(clonedSet(synced));
      onSynced();
    })().catch(() => undefined);
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- once per mount
  }, []);
  return slugs;
}

export default function Apps({ config }: { config: Config }) {
  const [apps, setApps] = useState<Loaded<AppInfo[]>>(
    catalogCache ? { status: "ok", data: catalogCache } : { status: "loading" },
  );
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
  // Bumped when the panel creates an app: refetches the grid without clearing it.
  const [nonce, setNonce] = useState(0);
  // One context-menu portal for the whole grid, at the cursor coords — same
  // shape as the explorer listing's (Listing.tsx openRowMenu).
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);

  const openCardMenu = (e: React.MouseEvent, app: AppInfo) => {
    // The card is an anchor: without this the browser's own link menu wins.
    e.preventDefault();
    // The card's thumb rides along as the export entry's capture crop source
    // (appShot, D396) — currentTarget is the card anchor the thumb sits in.
    // `[data-capture-ready]` is the card's own statement that the thumb has
    // PAINTED the app (AppPreviewCard sets it on the body iframe's load): only
    // two previews start at a time, so an unpainted thumb is the common case,
    // and cropping one would bake an empty box into the .fused as its permanent
    // thumbnail. No match → no crop source → appShot stages the app instead.
    const thumb = (e.currentTarget as Element).querySelector(
      ".app-pcard-thumb[data-capture-ready]",
    );
    setMenu({ x: e.clientX, y: e.clientY, items: appCardMenu(app, thumb) });
  };

  // Two fetches, in parallel, drawing the grid in two steps.
  //
  // The exhaustive catalog (GET /api/apps) is a recursive workspace walk plus
  // an index query — a few hundred ms cold — and the hub used to show nothing
  // but a skeleton for all of it. Home's row endpoint answers the same shape
  // from the two recents stores by explicit path, so firing it alongside puts
  // the apps the user actually uses on screen (and, more to the point, starts
  // their preview iframes, which is the slow part) while the walk finishes.
  //
  // PARALLEL, not sequential: the fast row is only fast for a user with a full
  // recents store — with fewer than FAST_ROW valid recents the server falls
  // back to the same exhaustive walk — so it must never be a gate in front of
  // the catalog. Its failure is likewise silent: the catalog is the answer,
  // this is a head start.
  useEffect(() => {
    let alive = true;
    getHomeApps(FAST_ROW).then(
      ({ apps: fast }) => {
        // Never overwrite a full grid — a cached one from a previous visit, or
        // a catalog that simply won this race.
        if (!alive || fast.length === 0) return;
        setApps((prev) => (prev.status === "loading" ? { status: "partial", data: fast } : prev));
      },
      () => undefined,
    );
    getApps().then(
      ({ apps }) => {
        if (!alive) return;
        catalogCache = apps;
        setError(null);
        setApps({ status: "ok", data: apps });
      },
      (e: Error) => alive && setError(e.message),
    );
    return () => {
      alive = false;
    };
  }, [nonce]);

  // Showcase apps are ordinary workspace apps now: the server clones the
  // community repo into <workspace>/showcase in the background on startup,
  // and the workspace scan picks it up like any other tag dir. No synthetic
  // chip, no separate catalog surface.
  //
  // `all` is the CATALOG — chips, the count and the empty state all speak for
  // the whole workspace, so they stay empty until the exhaustive answer lands
  // (a chip row derived from twelve recents would drop options as the rest
  // arrived, which reads as the page mis-drawing itself). `cards` is whatever
  // is drawable NOW, partial row included.
  const all = apps.status === "ok" ? apps.data : [];
  const cards = apps.status === "loading" ? [] : apps.data;
  // Folders chips, minus the exported `.fused` rows — see repoChips for why an
  // app FILE contributes none.
  const tags = useMemo(() => repoChips(all), [all]);
  // Categories scanned from the apps themselves (metadata.json `category`).
  // Apps without one carry null and so only ever appear under All. Ordered by
  // orderCategories: learn-first so the tutorial and starter chips lead the
  // row, then locale-alphabetical (which also replaces the code-unit sort the
  // Folders chips still use — see app-categories). Card order in the grid is
  // unaffected.
  const categories = useMemo(
    () => orderCategories(all.map((a) => a.category).filter((c): c is string => !!c)),
    [all],
  );
  const clonedSlugs = useShowcaseSync(() => setNonce((n) => n + 1));
  const q = query.trim().toLowerCase();
  const shown = useMemo(
    () =>
      sortApps(
        cards.filter(
          (a) =>
            (tag === null || a.tag === tag) &&
            (category === null || a.category === category) &&
            (q === "" ||
              a.name.toLowerCase().includes(q) ||
              (a.title ?? "").toLowerCase().includes(q) ||
              (a.category ?? "").toLowerCase().includes(q) ||
              a.tag.toLowerCase().includes(q)),
        ),
      ),
    [cards, tag, category, q],
  );
  const chips = mode === "repo" ? tags : categories;
  const active = mode === "repo" ? tag : category;

  return (
    <div className="apps-page">
      <div className="apps-inner">
        {/* Same hero as Home: prompt composer that names, scaffolds, and lands
            in the new app's claude chat. Creating from here refreshes the grid. */}
        <HomeHero onCreated={() => setNonce((n) => n + 1)} />

        {/* The hero's composer needs Claude Code, so the heads-up belongs
            wherever the hero does — this page is the other front door, not a
            second-class copy of Home. BELOW the hero here rather than above it:
            the wordmark is this page's masthead, and pushing it down the page
            would make a dismissible notice look like the app's own chrome.
            Renders nothing when there is nothing to say. */}
        <ClaudeHealthStrip />

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
        {/* Skeleton only while there is nothing drawable at all: once the fast
            row has landed the cards themselves are the loading indicator, and
            the count line below says the rest is still coming. */}
        {apps.status === "loading" && !error && <SkeletonLines rows={4} label="Loading apps" />}
        {apps.status !== "loading" && (
          <>
            <div className="apps-count">
              {apps.status === "partial"
                ? "Recently opened — loading all apps…"
                : shown.length === all.length
                  ? `${all.length} app${all.length === 1 ? "" : "s"}`
                  : `${shown.length} of ${all.length} apps`}
            </div>
            {shown.length === 0 ? (
              // Nothing to say yet during the partial phase: "no apps match" is
              // a claim about the whole catalog, which has not arrived.
              apps.status === "partial" ? null : (
                <div className="home-empty">
                  {all.length === 0
                    ? "No apps yet. Describe one in the composer above to create it."
                    : "No apps match — clear the search or filter."}
                </div>
              )
            ) : (
              <div className="apps-cards">
                {shown.map((app) => (
                  <AppPreviewCard
                    key={app.path}
                    app={app}
                    onContextMenu={openCardMenu}
                    badge={
                      app.tag === SHOWCASE_TAG && clonedSlugs.has(app.name) ? "cloned" : undefined
                    }
                  />
                ))}
              </div>
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
