// /marketplace/installed — what this machine has from the Marketplace: an
// "Updates" section (with "Update all") over the full "Installed" list. Rows
// are 64px: icon, name + one line, version, then Open / Update and a "⋯"
// (Uninstall, Show in Explorer, Open app page).
//
// The list is the catalog's installed entries plus whatever
// /api/marketplace/installed knows that the catalog does not (an app since
// removed from the repo still has a folder here, and must stay uninstallable).
//
// STAGE B (CONTRIB_LOOP.md §18.5/§18.7) — three kinds of row:
//   reviewed   (`installed.kind == "marketplace"`) a copy pinned to a
//              reviewed release. "Updates" lists the ones whose release
//              moved; Update there means "bring the waiting release in" —
//              the same check as the row's "Check for a new release"
//              (POST /api/marketplace/check). Undo of a release is on the
//              app page's Activity. "No longer in the Marketplace" when
//              delisted; a Blocked / Paused tag when Fused blocked its
//              release.
//   legacy     a plain-file install from the old Marketplace. It never
//              updates: "Switch to the reviewed version" (POST
//              /api/marketplace/adopt) when the app is listed again, else
//              "No longer updated".
//   unknown    an older server's stamp: as before.
import { useEffect, useMemo, useState } from "react";
import { BadgeCheck, FolderOpen, History, LayoutPanelTop, PackageOpen, RefreshCw, Trash2 } from "lucide-react";
import { AppIcon, Delayed, MoreMenuButton, RowSkeleton, VerifiedMark } from "@platform/ui/appstore";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { navigateUrl } from "@platform/lib/router";
import { isBrowserHandledClick } from "@platform/lib/appEntry";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import { useCurrentAppsChanged } from "@platform/lib/tasksChanged";
import {
  MARKETPLACE_HOME,
  actionFor,
  appDetailHref,
  appPageHref,
  categoryLabel,
  fetchInstalled,
  isActive,
  isLegacyCopy,
  isMarketCopy,
  isOwnCopy,
  postAdopt,
  refreshCatalog,
  releaseWaiting,
  showInstalledFolder,
  useChecking,
  useJobs,
  type AppEntry,
  type CatalogSnapshot,
} from "@platform/lib/marketplace";
import { notify } from "@platform/lib/notifications";
import { AppActionPill, EmptyState, MarketCard, openDetail } from "./shared";
import { CatalogNotices } from "./Discover";
import { checkRelease, runUpdateAll } from "./install-flow";

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

  const updates = apps.filter((a) => releaseWaiting(a) && a.compatible);
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
            {apps.length} {apps.length === 1 ? "app" : "apps"} from the Community
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
            body="Apps you get from the Community show up here, with each new release once Fused has reviewed it."
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
                  onClick={() => runUpdateAll(updates, jobs)}
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
  const checking = useChecking(app.slug);
  const [adopting, setAdopting] = useState(false);
  const inCatalog = !!snap.catalog?.apps.some((a) => a.slug === app.slug);
  const market = isMarketCopy(app);
  const legacy = isLegacyCopy(app);
  const own = isOwnCopy(app);
  const block = app.installed?.block ?? null;
  const delisted = !!app.installed?.delisted || (market && !inCatalog && !!snap.catalog);
  const path = action.path;
  const menu: MenuEntry[] = [];
  if (path) {
    menu.push({ label: "Open app page", icon: <LayoutPanelTop size={14} strokeWidth={2} />, onClick: () => navigateUrl(appPageHref(path)) });
    if (market) {
      // Undo of a release lives with the copy's Activity, on its app page.
      menu.push({
        label: "Activity and Undo…",
        icon: <History size={14} strokeWidth={2} />,
        onClick: () => navigateUrl(appPageHref(path)),
      });
    }
    menu.push({ label: "Show in Explorer", icon: <FolderOpen size={14} strokeWidth={2} />, onClick: () => showInstalledFolder(path) });
  }
  if (app.installed) {
    const p = app.installed.path;
    if (menu.length) menu.push("separator");
    menu.push({ label: "Uninstall…", icon: <Trash2 size={14} strokeWidth={2} />, danger: true, onClick: () => openUninstallApp({ path: p, name: app.name }) });
  }
  const have = app.installed?.version;
  const version =
    own ? (
      <>Your app</>
    ) : market && action.kind === "update" ? (
      <span className="mp-row-v-new">New release</span>
    ) : market ? (
      <>Reviewed</>
    ) : action.kind === "update" && have && have !== app.version ? (
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
        : own
          ? "Your app"
          : checking
          ? "Checking for a new release…"
          : block
            ? block.level === "hard"
              ? "Blocked by Fused"
              : "Updates paused by Fused"
            : market && delisted
              ? "No longer in the Community"
              : market && action.kind === "update"
                ? "A release is waiting. Update brings it in."
                : legacy
                  ? inCatalog
                    ? "Installed from the old Community"
                    : "No longer updated"
                  : app.installed?.modified
                    ? "Edited locally"
                    : !inCatalog
                      ? "No longer in the Community"
                      : null;
  const statusTone =
    action.kind === "retry" || block?.level === "hard"
      ? " is-error"
      : block || app.installed?.modified || (legacy && !inCatalog)
        ? " is-warn"
        : "";

  const adopt = async () => {
    if (adopting) return;
    setAdopting(true);
    try {
      const r = await postAdopt(app.slug);
      if (r.status === "error") notify({ title: r.message || r.error || "Couldn't switch this app", tone: "error" });
      else notify({ title: r.message || `${app.name} now gets reviewed releases. Your edits and data stayed.`, tone: "info" });
    } catch (e) {
      notify({ title: (e as Error).message || "Couldn't switch this app", tone: "error" });
    } finally {
      await refreshCatalog({ again: true }).catch(() => {});
      setAdopting(false);
    }
  };
  // Stretched link, as on the store cards: the name is the row's one anchor
  // (its overlay makes the whole row open the detail page), and the pill and
  // "⋯" sit above it as siblings rather than buttons nested in a link. An app
  // no longer in the catalog has no detail page, so its name is plain text.
  return (
    <div className={"as-row mp-row" + (inCatalog ? " is-linked" : "")}>
      <AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={40} />
      <span className="as-row-text">
        <span className="as-row-name">
          {inCatalog ? (
            <a
              className="as-row-link"
              href={appDetailHref(app.slug)}
              onClick={(e) => {
                if (e.defaultPrevented || isBrowserHandledClick(e)) return;
                e.preventDefault();
                openDetail(app.slug);
              }}
            >
              {app.name}
              {market && !delisted && block?.level !== "hard" && <VerifiedMark />}
            </a>
          ) : (
            <span className="as-row-title">
              {app.name}
              {market && !delisted && block?.level !== "hard" && <VerifiedMark />}
            </span>
          )}
          <span className="as-row-cat">{categoryLabel(snap.catalog, app.category)}</span>
        </span>
        <span className="as-row-desc">{app.description}</span>
      </span>
      <span className="mp-row-meta">
        <span className="mp-row-v">{version}</span>
        {status && (
          <span className={"mp-row-status" + statusTone} title={block?.reason}>
            {status}
          </span>
        )}
      </span>
      <span className="mp-row-actions">
        {legacy && inCatalog && (
          <button type="button" className="mp-btn is-small" onClick={() => void adopt()} disabled={adopting}>
            <BadgeCheck size={13} strokeWidth={2.2} aria-hidden="true" />
            {adopting ? "Switching…" : "Switch to the reviewed version"}
          </button>
        )}
        {market && !delisted && action.kind !== "update" && action.kind !== "progress" && (
          <button
            type="button"
            className="mp-btn is-small is-quiet"
            onClick={() => checkRelease(app)}
            disabled={checking}
            title="Check for a new release"
          >
            <RefreshCw size={13} strokeWidth={2.2} aria-hidden="true" className={checking ? "is-spinning" : ""} />
            <span className="mp-row-check-label">Check for a new release</span>
          </button>
        )}
        <AppActionPill app={app} size="sm" />
        {menu.length > 0 && <MoreMenuButton items={menu} label={`More actions for ${app.name}`} size="sm" />}
      </span>
    </div>
  );
}
