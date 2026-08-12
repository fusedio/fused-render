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
import { requestCloneApp } from "@platform/cloud/cloneApp";
import { useDeployEnabled } from "@platform/lib/prefs";
import ContextMenu, { type MenuEntry } from "@platform/ui/ContextMenu";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { AppPreviewCard } from "@apps/builder/AppPreviewCard";
import { CommunityGrid, COMMUNITY_TAG } from "@apps/builder/CommunityGrid";
import { useCommunityMountReady } from "@platform/lib/hooks";
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

export default function Apps({ config }: { config: Config }) {
  const [apps, setApps] = useState<Loaded<AppInfo[]>>({ status: "loading" });
  const [query, setQuery] = useState("");
  const [tag, setTag] = useState<string | null>(null);
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

  // The community tab lives alongside the workspace tags but is its own
  // surface (catalog cards, not workspace apps) — gated on the builtin
  // community mount so it never appears where the marketplace can't work.
  // Seeded from the boot config like the sidebar's entry — starting from
  // `false` made the chip lag behind the sidebar by a poll tick (~2s).
  const communityReady = useCommunityMountReady(config.community_mount_ready);

  const all = apps.status === "ok" ? apps.data : [];
  const tags = useMemo(() => {
    const t = [...new Set(all.map((a) => a.tag))].sort();
    return communityReady && !t.includes(COMMUNITY_TAG) ? [...t, COMMUNITY_TAG] : t;
  }, [all, communityReady]);
  // The chip only means the catalog when it's the appended one — a real
  // workspace tag dir named "community" keeps its normal filtering.
  const communityTab =
    tag === COMMUNITY_TAG && communityReady && !all.some((a) => a.tag === COMMUNITY_TAG);
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

        {communityTab ? (
          <CommunityGrid query={query} sort={sort} />
        ) : (
          <>
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
                  <AppPreviewCard key={app.path} app={app} onContextMenu={openCardMenu} />
                ))}
              </div>
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
