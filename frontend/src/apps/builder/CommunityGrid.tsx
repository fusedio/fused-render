// The /apps hub's "community" tab: cards for every app in the community
// catalog (docs/COMMUNITY_MARKETPLACE_SPEC.md). Unlike the workspace grid the
// thumbnail is the catalog's static preview.png, not a live iframe — these
// apps aren't on disk until previewed/cloned, and the sparse-checked browse
// set always has the png. Data comes from the marketplace backend:
// POST /api/run against the mounted community.py (the marketplace
// is html+py content; there is no dedicated REST surface, and community.py
// deliberately can't be imported by the server — it runs in the executor's
// user-code subprocess).
//
// Click = open: a cloned app opens its workspace copy (/apps/local/<name>,
// same in-app route as any local card); an uncloned one opens the live
// preview from the cache (/explorer/embed/… — a full page load, since the embed shell and
// the apps shell are different boot modes). Ordering is last-opened-first:
// community.py records a `touch` per open, merged with the app builder's own
// recents so opens of the cloned copy from the regular grid count too.
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
  // A cloned app opens its PAGE, like every other app card (appEntry.ts, D269) —
  // and like the uncloned branch just below, which has always opened the
  // catalog's index.html rather than the folder it sits in.
  if (app.installed && app.path) return urlForFsPath(`${app.path}/index.html`);
  // Uncloned: the preview page from the catalog cache, in the explorer's own
  // view route. Best-effort for middle-click/new-tab — the folder may not be
  // materialized yet (the left-click path materializes it via `detail` first).
  return urlForFsPath(`${cacheRoot ?? ""}/${app.slug}/index.html`);
}

async function openCommunityApp(app: CommunityApp, cacheRoot: string | undefined): Promise<void> {
  touch(app.slug);
  if (app.installed && app.path) {
    navigate(`${app.path}/index.html`, { isDir: false });
    return;
  }
  // Materialize the app folder in the cache (sparse checkout) before loading
  // the preview — the browse set only guarantees preview.png/metadata.json.
  const detail = await runCommunity<{ status?: string; message?: string; preview_entry?: string }>({
    action: "detail",
    slug: app.slug,
  });
  if (!detail.preview_entry) throw new Error("preview is not available for this app");
  navigate(detail.preview_entry);
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
  const [openError, setOpenError] = useState<string | null>(null);
  // An uncloned open first fetches the app's files from GitHub (the `detail`
  // materialize) before the preview can load — seconds of real work with no
  // navigation yet, so the card itself shows what's happening. Stays true
  // until the page unloads into the preview; only an error clears it.
  const [cloning, setCloning] = useState(false);
  const title = app.name || app.slug;
  const ago = timeAgo(openedAt || null);
  const href = hrefForCommunity(app, cacheRoot);
  const onClick = (e: React.MouseEvent) => {
    // Modified/middle clicks keep the browser's own behavior on the href.
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
      return;
    e.preventDefault();
    if (cloning) return; // one open in flight per card
    if (!(app.installed && app.path)) setCloning(true);
    setOpenError(null);
    openCommunityApp(app, cacheRoot).catch((err: Error) => {
      setCloning(false);
      setOpenError(err.message);
    });
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
          {openError && <span className="app-pcard-ago">{openError}</span>}
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
        {cloning && (
          // Not nested under the aria-hidden wrapper above — a screen reader
          // needs to hear this role="status" announce while cloning runs.
          <span className="app-pcard-cloning" role="status">
            <span className="mode-icon-spinner" />
            Cloning from GitHub…
          </span>
        )}
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
    return <SkeletonLines rows={4} label="Loading showcase apps" />;
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
