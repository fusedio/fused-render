// The /apps hub's "community" tab: cards for every app in the community
// catalog (docs/COMMUNITY_MARKETPLACE_SPEC.md). The catalog is a FULL clone
// of the community repo at <workspace>/showcase (community.py SHOWCASE_DIR),
// so every app — and its preview.png thumbnail — is already on disk once the
// first refresh's clone finishes. Data comes from the marketplace backend
// (POST /api/community).
//
// Click = open: a cloned app opens its workspace copy; an uncloned one opens
// the app in place in the showcase clone, where it is fully editable (the
// preview's Clone button copies it to Fused/local to keep). Ordering is
// last-opened-first: community.py records a `touch` per open, merged with the
// app builder's own recents so opens of the cloned copy from the regular grid
// count too.
import { useEffect, useMemo, useState } from "react";
import { getJson, rawUrl } from "@platform/lib/api";
import { runCommunity, touchCommunityApp as touch } from "@platform/lib/community";
import { navigate, urlForFsPath } from "@platform/lib/router";
import { timeAgo } from "@platform/lib/format";
import { ErrorBanner } from "@platform/ui/ErrorBanner";
import { SkeletonLines } from "@platform/ui/Skeleton";
import { hueFor } from "@apps/builder/AppCard";

export const COMMUNITY_TAG = "community";

export interface CommunityApp {
  slug: string;
  name?: string;
  description?: string;
  installed?: boolean;
  path?: string; // the cloned copy's folder, when installed
  installed_at?: string;
  opened_at?: number | null; // epoch seconds of the last open recorded by `touch`
  yanked?: boolean;
}

interface Catalog {
  status: string;
  message?: string;
  cache_root?: string;
  apps?: CommunityApp[];
}

// -- catalog loading (stale-while-revalidate) -----------------------------

// Module-scope cache: switching tabs within a session re-renders instantly
// from the last catalog; a background refresh (once per session) folds in
// upstream changes.
let cachedCatalog: Catalog | null = null;
let refreshedThisSession = false;

type Loaded =
  | { status: "loading" }
  | { status: "ok"; catalog: Catalog }
  | { status: "error"; message: string };

function useCommunityCatalog(): Loaded {
  const [state, setState] = useState<Loaded>(
    cachedCatalog ? { status: "ok", catalog: cachedCatalog } : { status: "loading" },
  );
  useEffect(() => {
    let alive = true;
    const apply = (catalog: Catalog) => {
      cachedCatalog = catalog;
      if (alive) setState({ status: "ok", catalog });
    };
    const fail = (e: Error) => {
      if (alive && !cachedCatalog) setState({ status: "error", message: e.message });
    };
    (async () => {
      try {
        // Always re-join on mount, even when a catalog is already cached:
        // `catalog` is a cheap local read (installs.json + index.json, no
        // network), and install/update/uninstall performed elsewhere only
        // shows up here through a fresh read of it.
        // Without this, this tab kept the install flags from whenever it
        // first mounted this session.
        const catalog = await runCommunity<Catalog>({ action: "catalog" });
        if (catalog.status === "ok") apply(catalog);
        // no-cache (first run ever): fall through to the refresh below,
        // which clones the catalog repo — that's the slow path the
        // skeleton covers.
        if (!refreshedThisSession) {
          refreshedThisSession = true;
          apply(await runCommunity<Catalog>({ action: "refresh" }));
        }
      } catch (e) {
        refreshedThisSession = false; // a failed refresh may be retried next visit
        fail(e as Error);
      }
    })();
    return () => {
      alive = false;
    };
  }, []);
  return state;
}

// The app builder's own recents (~/.fused-render/app_recents.json): opens of
// a CLONED community app happen through the regular grid too, and only this
// store sees those. Merged into the ordering so "last opened" means opened
// anywhere, not just via this tab.
function useLocalOpens(): Map<string, number> {
  const [opens, setOpens] = useState<Map<string, number>>(new Map());
  useEffect(() => {
    let alive = true;
    getJson<{ entries: { tag: string; name: string; openedAt: string }[] }>("/api/apps/recents")
      .then(({ entries }) => {
        if (!alive) return;
        const m = new Map<string, number>();
        for (const e of entries) {
          if (e.tag !== "local") continue;
          const t = Date.parse(e.openedAt);
          if (!Number.isNaN(t)) m.set(e.name, t / 1000);
        }
        setOpens(m);
      })
      .catch(() => undefined); // ordering metadata only
    return () => {
      alive = false;
    };
  }, []);
  return opens;
}

function lastOpened(app: CommunityApp, localOpens: Map<string, number>): number {
  const local = app.installed && app.path ? (localOpens.get(basename(app.path)) ?? 0) : 0;
  return Math.max(app.opened_at ?? 0, local);
}

function basename(p: string): string {
  const parts = p.replace(/[\\/]+$/, "").split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

// -- open behavior ---------------------------------------------------------

function hrefForCommunity(app: CommunityApp, cacheRoot: string | undefined): string {
  // A cloned app opens its workspace folder in the explorer — same rule as a
  // workspace card (appEntry.ts, D262: no app route, the listing is the
  // destination).
  if (app.installed && app.path) return urlForFsPath(app.path);
  // Uncloned: the app's page in the showcase clone — a full clone, so the
  // files are already on disk.
  return urlForFsPath(`${cacheRoot ?? ""}/${app.slug}/index.html`);
}

function openCommunityApp(app: CommunityApp, cacheRoot: string | undefined): void {
  touch(app.slug);
  if (app.installed && app.path) {
    navigate(app.path, { isDir: true });
    return;
  }
  navigate(`${cacheRoot ?? ""}/${app.slug}/index.html`);
}

// -- components -------------------------------------------------------------

function CommunityCard({
  app,
  cacheRoot,
  openedAt,
}: {
  app: CommunityApp;
  cacheRoot: string | undefined;
  openedAt: number;
}) {
  const [imgFailed, setImgFailed] = useState(false);
  const title = app.name || app.slug;
  const ago = timeAgo(openedAt || null);
  const href = hrefForCommunity(app, cacheRoot);
  const onClick = (e: React.MouseEvent) => {
    // Modified/middle clicks keep the browser's own behavior on the href.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
      return;
    e.preventDefault();
    openCommunityApp(app, cacheRoot);
  };
  return (
    <a className="app-pcard" href={href} onClick={onClick} title={app.description || title}>
      <span className="app-pcard-body">
        <span className="app-pcard-title">{title}</span>
        <span className="app-pcard-meta">
          {/* Display name only — the tag VALUE stays "community" everywhere
              (dirs, ?tag=, community.py). */}
          <span className="app-pcard-tag">showcase</span>
          {app.installed && <span className="app-pcard-name">cloned</span>}
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
      <span className="app-pcard-thumb">
        <span aria-hidden="true">
          {cacheRoot && !imgFailed ? (
            <img
              className="app-pcard-shot"
              src={rawUrl(`${cacheRoot}/${app.slug}/preview.png`)}
              loading="lazy"
              alt=""
              onError={() => setImgFailed(true)}
            />
          ) : (
            <span className="app-pcard-monogram" style={{ color: hueFor(title) }}>
              {title.charAt(0).toUpperCase()}
            </span>
          )}
        </span>
      </span>
    </a>
  );
}

export function CommunityGrid({ query, sort }: { query: string; sort: "recent" | "name" }) {
  const catalog = useCommunityCatalog();
  const localOpens = useLocalOpens();
  const q = query.trim().toLowerCase();
  const apps = catalog.status === "ok" ? (catalog.catalog.apps ?? []) : [];
  const cacheRoot = catalog.status === "ok" ? catalog.catalog.cache_root : undefined;
  const shown = useMemo(() => {
    const byName = (a: CommunityApp, b: CommunityApp) =>
      (a.name || a.slug).localeCompare(b.name || b.slug) || a.slug.localeCompare(b.slug);
    const filtered = apps.filter(
      (a) =>
        q === "" ||
        a.slug.toLowerCase().includes(q) ||
        (a.name ?? "").toLowerCase().includes(q) ||
        (a.description ?? "").toLowerCase().includes(q),
    );
    if (sort === "name") return filtered.sort(byName);
    // "recent" = last-opened desc; never-opened apps sink, alphabetical.
    return filtered.sort(
      (a, b) => lastOpened(b, localOpens) - lastOpened(a, localOpens) || byName(a, b),
    );
  }, [apps, q, sort, localOpens]);

  if (catalog.status === "error") return <ErrorBanner>{catalog.message}</ErrorBanner>;
  if (catalog.status === "loading")
    // The very first load clones the whole showcase repo — that's the slow
    // path this skeleton covers.
    return <SkeletonLines rows={4} label="Loading showcase apps (first load clones the catalog)" />;
  return (
    <>
      <div className="apps-count">
        {shown.length === apps.length
          ? `${apps.length} community app${apps.length === 1 ? "" : "s"}`
          : `${shown.length} of ${apps.length} community apps`}
      </div>
      {shown.length === 0 ? (
        <div className="home-empty">
          {apps.length === 0
            ? "No community apps in the catalog yet."
            : "No community apps match — clear the search."}
        </div>
      ) : (
        <div className="apps-cards">
          {shown.map((app) => (
            <CommunityCard
              key={app.slug}
              app={app}
              cacheRoot={cacheRoot}
              openedAt={lastOpened(app, localOpens)}
            />
          ))}
        </div>
      )}
    </>
  );
}
