// /marketplace/installed — what this machine has from the Marketplace: an
// "Updates" section (with "Update all") over the full "Installed" list. Rows
// are 64px: icon, name + one line, version, then Open / Update and a "⋯"
// (Uninstall, Show in Explorer, Open app page).
//
// The list is the catalog's installed entries plus whatever
// /api/marketplace/installed knows that the catalog does not (an app since
// removed from the repo still has a folder here, and must stay uninstallable).
import { useEffect, useMemo, useState } from "react";
import { FolderOpen, LayoutPanelTop, PackageOpen, RefreshCw, Trash2 } from "lucide-react";
import { AppIcon, Delayed, MoreMenuButton, RowSkeleton } from "@platform/ui/appstore";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { navigateUrl } from "@platform/lib/router";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { useCurrentAppsChanged } from "@platform/lib/tasksChanged";
import {
  MARKETPLACE_HOME,
  actionFor,
  appDetailHref,
  appPageHref,
  categoryLabel,
  fetchInstalled,
  installApp,
  isActive,
  showInstalledFolder,
  useJobs,
  type AppEntry,
  type CatalogSnapshot,
} from "@platform/lib/marketplace";
import { AppActionPill, EmptyState, MarketCard, openDetail } from "./shared";
import { CatalogNotices } from "./Discover";

export function InstalledPage({ snap }: { snap: CatalogSnapshot }) {
  const [extra, setExtra] = useState<AppEntry[] | null>(null);
  const [tick, setTick] = useState(0);
  useCurrentAppsChanged(() => setTick((t) => t + 1));
  useEffect(() => {
    let live = true;
    fetchInstalled()
      .then((r) => live && setExtra(r.apps ?? []))
      .catch(() => live && setExtra([]));
    return () => {
      live = false;
    };
  }, [tick, snap.catalog]);
  const jobs = useJobs();

  const apps = useMemo(() => {
    const fromCatalog = (snap.catalog?.apps ?? []).filter((a) => a.installed || jobs[a.slug]?.state === "done");
    const known = new Set(fromCatalog.map((a) => a.slug));
    const orphans = (extra ?? []).filter((a) => !known.has(a.slug) && a.installed);
    return fromCatalog.concat(orphans).sort((a, b) => a.name.localeCompare(b.name));
  }, [snap.catalog, extra, jobs]);

  const updates = apps.filter((a) => a.update_available && a.compatible);
  // Each app is listed once: an app with an update lives in Updates (and
  // stays there, ring and all, while it updates — the catalog re-read that
  // clears `update_available` moves it down once it lands).
  const current = apps.filter((a) => !updates.includes(a));
  const updatingAll = updates.length > 0 && updates.every((a) => isActive(jobs[a.slug]));
  const loading = !snap.catalog && !snap.error;
  // Nothing installed: suggest the featured apps (then starters) rather than
  // leaving a lone empty state on a wide page.
  const picks = useMemo(() => {
    const all = snap.catalog?.apps ?? [];
    const featured = all.filter((a) => a.featured);
    const starters = all.filter((a) => a.category === "starters" && !a.featured);
    return featured.concat(starters).slice(0, 6);
  }, [snap.catalog]);

  return (
    <div className="mp-installed">
      <CatalogNotices snap={snap} />
      <div className="mp-page-title">
        <h1>Installed</h1>
        {apps.length > 0 && (
          <span className="mp-page-sub">
            {apps.length} {apps.length === 1 ? "app" : "apps"} from the Marketplace
          </span>
        )}
      </div>

      {loading ? (
        <Delayed>
          <div className="mp-list">
            {Array.from({ length: 4 }, (_, k) => (
              <RowSkeleton key={k} />
            ))}
          </div>
        </Delayed>
      ) : apps.length === 0 ? (
        <>
          <EmptyState
            icon={<PackageOpen size={24} strokeWidth={1.7} />}
            title="Nothing installed yet"
            body="Apps you get from the Marketplace show up here, with updates as their authors publish them."
            action={
              <button type="button" className="mp-btn is-primary" onClick={() => navigateUrl(MARKETPLACE_HOME)}>
                Browse apps
              </button>
            }
          />
          {picks.length > 0 && (
            <section className="mp-shelf" aria-labelledby="mp-picks">
              <div className="mp-shelf-head">
                <h2 className="mp-shelf-title" id="mp-picks">
                  Good places to start
                </h2>
              </div>
              <div className="mp-grid is-compact">
                {picks.map((a) => (
                  <MarketCard key={a.slug} app={a} catalog={snap.catalog} variant="compact" />
                ))}
              </div>
            </section>
          )}
        </>
      ) : (
        <>
          {updates.length > 0 && (
            <section className="mp-list-section" aria-labelledby="mp-updates">
              <div className="mp-shelf-head">
                <h2 className="mp-shelf-title" id="mp-updates">
                  Updates <span className="mp-count-badge">{updates.length}</span>
                </h2>
                <button
                  type="button"
                  className="mp-btn is-primary is-small"
                  disabled={updatingAll}
                  onClick={() => {
                    for (const a of updates) {
                      if (!isActive(jobs[a.slug])) installApp({ slug: a.slug, name: a.name }).catch(() => {});
                    }
                  }}
                >
                  <RefreshCw size={13} strokeWidth={2.3} aria-hidden="true" className={updatingAll ? "is-spinning" : ""} />
                  {updatingAll ? "Updating…" : "Update all"}
                </button>
              </div>
              <div className="mp-list">
                {updates.map((a) => (
                  <InstalledRow key={a.slug} app={a} snap={snap} />
                ))}
              </div>
            </section>
          )}
          {current.length > 0 && (
            <section className="mp-list-section" aria-labelledby="mp-installed-all">
              <div className="mp-shelf-head">
                <h2 className="mp-shelf-title" id="mp-installed-all">
                  {updates.length > 0 ? "Up to date" : "Installed apps"}
                </h2>
              </div>
              <div className="mp-list">
                {current.map((a) => (
                  <InstalledRow key={a.slug} app={a} snap={snap} />
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}

function InstalledRow({ app, snap }: { app: AppEntry; snap: CatalogSnapshot }) {
  const jobs = useJobs();
  const job = jobs[app.slug] ?? null;
  const action = actionFor(app, job);
  const inCatalog = !!snap.catalog?.apps.some((a) => a.slug === app.slug);
  const path = action.path;
  const menu: MenuEntry[] = [];
  if (path) {
    menu.push({ label: "Open app page", icon: <LayoutPanelTop size={14} strokeWidth={2} />, onClick: () => navigateUrl(appPageHref(path)) });
    menu.push({ label: "Show in Explorer", icon: <FolderOpen size={14} strokeWidth={2} />, onClick: () => showInstalledFolder(path) });
  }
  if (app.installed) {
    const p = app.installed.path;
    if (menu.length) menu.push("separator");
    menu.push({ label: "Uninstall…", icon: <Trash2 size={14} strokeWidth={2} />, danger: true, onClick: () => openUninstallApp({ path: p, name: app.name }) });
  }
  const have = app.installed?.version;
  const version =
    action.kind === "update" && have ? (
      <>
        <span className="mp-row-v-old">{have}</span>
        <span aria-hidden="true"> → </span>
        <span className="mp-row-v-new">{app.version}</span>
      </>
    ) : (
      <>v{have ?? app.version}</>
    );
  const status =
    action.kind === "progress"
      ? job?.message || "Installing…"
      : action.kind === "retry"
        ? action.error
        : app.installed?.modified
          ? "Edited locally"
          : !inCatalog
            ? "No longer in the Marketplace"
            : null;
  return (
    <div
      className="as-row mp-row"
      role="link"
      tabIndex={inCatalog ? 0 : -1}
      aria-label={app.name}
      onClick={() => inCatalog && openDetail(app.slug)}
      onKeyDown={(e) => {
        if (e.key === "Enter" && inCatalog && e.target === e.currentTarget) openDetail(app.slug);
      }}
      data-href={inCatalog ? appDetailHref(app.slug) : undefined}
    >
      <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={40} />
      <span className="as-row-text">
        <span className="as-row-name">
          {app.name}
          <span className="as-row-cat">{categoryLabel(snap.catalog, app.category)}</span>
        </span>
        <span className="as-row-desc">{app.description}</span>
      </span>
      <span className="mp-row-meta">
        <span className="mp-row-v">{version}</span>
        {status && <span className={"mp-row-status" + (action.kind === "retry" ? " is-error" : app.installed?.modified ? " is-warn" : "")}>{status}</span>}
      </span>
      <span className="mp-row-actions">
        <AppActionPill app={app} size="sm" />
        {menu.length > 0 && <MoreMenuButton items={menu} label={`More actions for ${app.name}`} size="sm" />}
      </span>
    </div>
  );
}
