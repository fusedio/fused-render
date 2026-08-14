// Apps hub — lives at "/apps", chrome-free like Home (no sidebar, no
// breadcrumb). Every detected app in the workspace (GET /api/apps) as a grid
// of big preview cards — each thumbnail is the app itself rendered in a
// scaled, non-interactive iframe (AppPreviewCard). The list is narrowed by a
// filter row — a Category/Repo mode selector with chips derived from the apps
// themselves (categories from each folder's metadata.json, repos from the
// top-level tag dirs) — and a search box (name/title/tag/category,
// case-insensitive). Order is always recently-modified; filtering never
// reorders cards relative to each other.
import { useEffect, useMemo, useState } from "react";
import { getApps } from "@platform/lib/api";
import type { AppInfo, Config } from "@platform/lib/api";
import { appCardMenu } from "@platform/lib/appCardMenu";
import { runCommunity, SHOWCASE_TAG } from "@platform/lib/community";
import { requestCloneApp } from "@platform/cloud/cloneApp";
import { useDeployEnabled } from "@platform/lib/prefs";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppPreviewCard } from "@apps/builder/AppPreviewCard";
import { useNavEpoch } from "@platform/lib/hooks";
import { navigateUrl } from "@platform/lib/router";
import { HomeHero } from "./HomeHero";
import { SkeletonLines } from "@platform/ui/Skeleton";

type Loaded<T> = { status: "loading" } | { status: "ok"; data: T } | { status: "error"; message: string };

// Which facet the chips filter by. "category" reads each app's authored
// metadata.json category; "repo" is the top-level workspace folder (tag):
// all / examples / local / showcase in a stock workspace.
type FilterMode = "category" | "repo";

const MODES: { key: FilterMode; label: string }[] = [
  { key: "category", label: "Category" },
  { key: "repo", label: "Repo" },
];

// Always recently-modified desc; apps without a timestamp sink to the end,
// name breaks ties so the order is stable.
function sortApps(apps: AppInfo[]): AppInfo[] {
  const byName = (a: AppInfo, b: AppInfo) =>
    (a.title || a.name).localeCompare(b.title || b.name) || a.name.localeCompare(b.name);
  return apps.slice().sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || byName(a, b));
}

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
  const [apps, setApps] = useState<Loaded<AppInfo[]>>({ status: "loading" });
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
  // Whether deploying is switched on at all — the import entry follows it (see the toolbar).
  const deployEnabled = useDeployEnabled();
  // Bumped when the panel creates an app: refetches the grid without clearing it.
  const [nonce, setNonce] = useState(0);
  // One context-menu portal for the whole grid, at the cursor coords — same
  // shape as the explorer listing's (Listing.tsx openRowMenu).
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuEntry[] } | null>(null);

  const openCardMenu = (e: React.MouseEvent, app: AppInfo) => {
    // The card is an anchor: without this the browser's own link menu wins.
    e.preventDefault();
    setMenu({ x: e.clientX, y: e.clientY, items: appCardMenu(app) });
  };

  useEffect(() => {
    let alive = true;
    getApps().then(
      ({ apps }) => alive && setApps({ status: "ok", data: apps }),
      (e: Error) => alive && setApps({ status: "error", message: e.message }),
    );
    return () => {
      alive = false;
    };
  }, [nonce]);

  // Showcase apps are ordinary workspace apps now: the server clones the
  // community repo into <workspace>/showcase in the background on startup,
  // and the workspace scan picks it up like any other tag dir. No synthetic
  // chip, no separate catalog surface.
  const all = apps.status === "ok" ? apps.data : [];
  const tags = useMemo(() => [...new Set(all.map((a) => a.tag))].sort(), [all]);
  // Categories scanned from the apps themselves (metadata.json `category`).
  // Apps without one carry null and so only ever appear under All.
  const categories = useMemo(
    () => [...new Set(all.map((a) => a.category).filter((c): c is string => !!c))].sort(),
    [all],
  );
  const clonedSlugs = useShowcaseSync(() => setNonce((n) => n + 1));
  const q = query.trim().toLowerCase();
  const shown = useMemo(
    () =>
      sortApps(
        all.filter(
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
    [all, tag, category, q],
  );
  const chips = mode === "repo" ? tags : categories;
  const active = mode === "repo" ? tag : category;

  return (
    <div className="apps-page">
      <div className="apps-inner">
        {/* Same hero as Home: prompt composer that names, scaffolds, and lands
            in the new app's claude chat. Creating from here refreshes the grid. */}
        <HomeHero onCreated={() => setNonce((n) => n + 1)} />

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
            <div className="apps-tags" role="group" aria-label={`Filter by ${mode}`}>
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
          {/* Gated on the Deploy-apps preference (SPEC §35 CL-1): with deploying switched
              off the whole surface that produces these links is hidden, so an entry for
              importing one would advertise a feature the user has turned away from. The
              path bar's pasted-link route stays available regardless — refusing a URL the
              user explicitly pasted is a worse failure than showing one extra button. */}
          {deployEnabled && (
            <button type="button" className="btn" onClick={() => requestCloneApp()}>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
                <polyline points="7 10 12 15 17 10" />
                <line x1="12" y1="15" x2="12" y2="3" />
              </svg>
              Open deployed app
            </button>
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

        {apps.status === "error" && <ErrorBanner>{apps.message}</ErrorBanner>}
        {apps.status === "loading" && <SkeletonLines rows={4} label="Loading apps" />}
        {apps.status === "ok" && (
          <>
            <div className="apps-count">
              {shown.length === all.length
                ? `${all.length} app${all.length === 1 ? "" : "s"}`
                : `${shown.length} of ${all.length} apps`}
            </div>
            {shown.length === 0 ? (
              <div className="home-empty">
                {all.length === 0
                  ? "No apps yet. Describe one in the composer above to create it."
                  : "No apps match — clear the search or filter."}
              </div>
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
