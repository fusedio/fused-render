// Apps hub — lives at "/apps", chrome-free like Home (no sidebar, no
// breadcrumb). Every detected app in the workspace (GET /api/apps) as a grid
// of big preview cards — each thumbnail is the app itself rendered in a
// scaled, non-interactive iframe (AppPreviewCard). The list is narrowed by a
// search box (name/title/tag, case-insensitive) and tag chips derived from
// the apps themselves, and ordered by an explicit sort control (recently
// modified / name). Sorting is a deliberate user action — filtering alone
// never reorders cards under interaction.
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

type SortKey = "recent" | "name";

const SORTS: { key: SortKey; label: string }[] = [
  { key: "recent", label: "Recent" },
  { key: "name", label: "Name" },
];

function sortApps(apps: AppInfo[], sort: SortKey): AppInfo[] {
  const byName = (a: AppInfo, b: AppInfo) =>
    (a.title || a.name).localeCompare(b.title || b.name) || a.name.localeCompare(b.name);
  const sorted = apps.slice();
  if (sort === "name") sorted.sort(byName);
  // "recent" = last-modified desc; apps without a timestamp sink to the end.
  else sorted.sort((a, b) => (b.updated_at ?? 0) - (a.updated_at ?? 0) || byName(a, b));
  return sorted;
}

type ShowcaseCatalog = { status?: string; apps?: { slug: string; installed?: boolean }[] };

const clonedSet = (c: ShowcaseCatalog) =>
  new Set((c.apps ?? []).filter((a) => a.installed).map((a) => a.slug));

// Read the showcase install records once per mount; feeds the "cloned"
// badges on showcase cards.
//
// `catalog` first — a cheap local read (installs.json + index.json), no lock,
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
  // The selected tag lives in the URL (`?tag=`), not in state — the AiModels
  // pattern: it makes the filter bookmarkable, survives a reload, and puts the
  // choice on the back button. useNavEpoch counts pushState and popstate
  // alike, so back/forward re-reads the URL. Default (All) is the ABSENCE of
  // the param, keeping /apps the clean URL for the page.
  const navEpoch = useNavEpoch();
  const tag = useMemo(() => new URLSearchParams(location.search).get("tag"), [navEpoch]);
  const setTag = (next: string | null) => {
    if (next === tag) return;
    const params = new URLSearchParams(location.search);
    if (next === null) params.delete("tag");
    else params.set("tag", next);
    const search = params.toString();
    // navigateUrl (pushState), not replaceSearch: each tag selection is a
    // history entry so back/forward walks the filter history.
    navigateUrl(location.pathname + (search ? "?" + search : ""));
  };
  const [sort, setSort] = useState<SortKey>("recent");
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
  // and the two-level scan picks it up like any other tag dir. No synthetic
  // chip, no separate catalog surface.
  const all = apps.status === "ok" ? apps.data : [];
  const tags = useMemo(() => [...new Set(all.map((a) => a.tag))].sort(), [all]);
  const clonedSlugs = useShowcaseSync(() => setNonce((n) => n + 1));
  const q = query.trim().toLowerCase();
  const shown = useMemo(
    () =>
      sortApps(
        all.filter(
          (a) =>
            (tag === null || a.tag === tag) &&
            (q === "" ||
              a.name.toLowerCase().includes(q) ||
              (a.title ?? "").toLowerCase().includes(q) ||
              a.tag.toLowerCase().includes(q)),
        ),
        sort,
      ),
    [all, tag, q, sort],
  );

  return (
    <div className="apps-page">
      <div className="apps-inner">
        {/* Same hero as Home: prompt composer that names, scaffolds, and lands
            in the new app's claude chat. Creating from here refreshes the grid. */}
        <HomeHero onCreated={() => setNonce((n) => n + 1)} />

        <div className="apps-toolbar">
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
          <div className="apps-sort" role="group" aria-label="Sort apps">
            {SORTS.map((s) => (
              <button
                key={s.key}
                type="button"
                className={"apps-sort-btn" + (sort === s.key ? " is-active" : "")}
                onClick={() => setSort(s.key)}
              >
                {s.label}
              </button>
            ))}
          </div>
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
          {tags.length > 0 && (
            <div className="apps-tags" role="group" aria-label="Filter by tag">
              <button
                type="button"
                className={"apps-tag-chip" + (tag === null ? " is-active" : "")}
                onClick={() => setTag(null)}
              >
                All
              </button>
              {tags.map((t) => (
                <button
                  key={t}
                  type="button"
                  className={"apps-tag-chip" + (tag === t ? " is-active" : "")}
                  onClick={() => setTag(tag === t ? null : t)}
                >
                  {t}
                </button>
              ))}
            </div>
          )}
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
                  : "No apps match — clear the search or tag filter."}
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
