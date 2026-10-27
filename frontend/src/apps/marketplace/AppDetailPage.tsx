// /marketplace/app/<slug> — one app, App-Store style, built from the shared
// detail blocks in @platform/ui/appstore (the `.fused` install page,
// FusedFilePage.tsx, uses the same ones): hero with the action and a "⋯" menu, the Information
// strip, the screenshot stage, Overview (readme) / Files tabs beside an
// Information list and the "Before you install" card, then "More in
// <Category>".
//
// Readme polish (OverviewTabs' trimReadme): a readme almost always opens with
// `# <Name>` and the very screenshot the stage already shows — both are
// dropped from the rendered copy, so the Overview starts with the prose.
//
// Stage B (CONTRIB_LOOP.md §18.7): the byline credits a remix's original
// ("Remix of <name>", a link to its page), the strip shows the last release,
// remixes and contributors, and a Releases section lists every release with
// its What's new — "Blocked" where Fused blocked it since.
import type { ReactNode } from "react";
import { ArrowUpRight, ExternalLink, FolderOpen, LayoutPanelTop, Trash2 } from "lucide-react";
import {
  AppIcon,
  DetailBody,
  DetailHero,
  DetailLayout,
  DetailSection,
  DetailSkeleton,
  Delayed,
  InfoList,
  InfoStrip,
  OverviewTabs,
  PreviewStage,
  TagsSection,
  TrustCard,
  VerifiedMark,
  plainClick,
  type InfoStripItem,
} from "@platform/ui/appstore";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { formatSize, releaseDate } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import {
  MARKETPLACE_HOME,
  actionFor,
  appDetailHref,
  appPageHref,
  isMarketCopy,
  isOwnCopy,
  categoryLabel,
  showInstalledFolder,
  sourceUrlOf,
  useAppDetail,
  useJob,
  type AppEntry,
  type CatalogSnapshot,
  type MarketRelease,
} from "@platform/lib/marketplace";
import { AppActionPill, CategoryGlyph, EmptyState, MarketCard, compactCount, openDetail, remixOf } from "./shared";
import { trustItemsFor } from "./trust";

/** U+202F NARROW NO-BREAK SPACE: "12.4 MB" never wraps between number and
 *  unit in the info strip's narrow cell, and sits tighter than a full NBSP. */
const NNBSP = "\u202f";

function githubUrl(handle: string): string {
  return `https://github.com/${encodeURIComponent(handle.replace(/^@/, ""))}`;
}

export function AppDetailPage({ slug, snap, bare = false }: { slug: string; snap: CatalogSnapshot; bare?: boolean }) {
  const { detail, error, reload } = useAppDetail(slug);
  const fromCatalog = snap.catalog?.apps.find((a) => a.slug === slug) ?? null;
  const app: AppEntry | null = detail?.app ?? fromCatalog;
  const job = useJob(slug);

  // Bare (a listing link with the store off): nothing leads into the store.
  const back = bare
    ? undefined
    : {
        label: "Community",
        href: MARKETPLACE_HOME,
        onClick: plainClick(() => navigateUrl(MARKETPLACE_HOME)),
      };

  if (!app) {
    return (
      <DetailLayout back={back}>
        {error ? (
          <EmptyState
            title={error.includes("404") || /not found/i.test(error) ? "App not found" : "Couldn't load this app"}
            body={
              /not found|404/i.test(error)
                ? `There is no app called “${slug}” in the Community.`
                : error
            }
            action={
              <button type="button" className="mp-btn" onClick={reload}>
                Try again
              </button>
            }
          />
        ) : (
          <Delayed>
            <DetailSkeleton />
          </Delayed>
        )}
      </DetailLayout>
    );
  }

  const action = actionFor(app, job);
  const installedPath = action.path;
  const cat = categoryLabel(snap.catalog, app.category);
  const more = (snap.catalog?.apps ?? []).filter((a) => a.category === app.category && a.slug !== app.slug).slice(0, 6);

  // Only a github.com address is linked (sourceUrlOf): the row's string is
  // the catalog's, not ours.
  const sourceUrl = sourceUrlOf(app);
  const menu: MenuEntry[] = [];
  if (installedPath) {
    menu.push({
      label: "Open app page",
      icon: <LayoutPanelTop size={14} strokeWidth={2} />,
      onClick: () => navigateUrl(appPageHref(installedPath)),
    });
    menu.push({
      label: "Show in Explorer",
      icon: <FolderOpen size={14} strokeWidth={2} />,
      onClick: () => showInstalledFolder(installedPath),
    });
  }
  if (sourceUrl) {
    const src = sourceUrl;
    menu.push({
      label: "View source on GitHub",
      icon: <ExternalLink size={14} strokeWidth={2} />,
      onClick: () => window.open(src, "_blank", "noopener"),
    });
  }
  if (app.installed) {
    const p = app.installed.path;
    menu.push("separator");
    menu.push({
      label: "Uninstall…",
      icon: <Trash2 size={14} strokeWidth={2} />,
      danger: true,
      onClick: () => openUninstallApp({ path: p, name: app.name }),
    });
  }

  const block = app.installed?.block ?? null;
  // Every listed row passed the review check; a hard block withdraws the mark.
  const verified = block?.level !== "hard";
  const market = isMarketCopy(app);
  let note: ReactNode = null;
  if (block) {
    note = (
      <span className="is-warn" title={block.reason}>
        {block.level === "hard" ? "Blocked by Fused · see the app page" : "Updates paused by Fused"}
      </span>
    );
  } else if (action.kind === "open" && isOwnCopy(app)) {
    note = <>Your app · you publish its releases</>;
  } else if (action.kind === "update" && market) {
    note = (
      <>
        A new release is waiting{releaseDate(app.last_release_at) ? <> · {releaseDate(app.last_release_at)}</> : null}
      </>
    );
  } else if (action.kind === "open" && market) {
    note = app.installed?.delisted ? (
      <span className="is-warn">Installed · no longer in the Community</span>
    ) : (
      <>Installed · reviewed release</>
    );
  } else if (action.kind === "update") {
    note = (
      <>
        Update to <b>{app.version}</b>
        {app.installed?.version ? <> · you have {app.installed.version}</> : null}
      </>
    );
  } else if (action.kind === "open" && app.installed) {
    note = app.installed.modified ? (
      <span className="is-warn" title="Local files differ from what was installed; an update would replace them.">
        Installed · edited locally
      </span>
    ) : (
      <>Installed{app.installed.version ? <> · v{app.installed.version}</> : null}</>
    );
  } else if (action.kind === "retry") {
    note = <span className="is-error">{action.error}</span>;
  } else if (action.kind === "progress") {
    note = job?.message || (job?.state === "installing" ? "Installing…" : "Downloading…");
  } else if (action.kind === "get") {
    note = app.size_bytes ? `${formatSize(app.size_bytes)} · Free` : "Free";
  } else if (action.kind === "incompatible") {
    note = <span className="is-warn">Needs Fused Render {app.min_fused_render ?? "newer"}</span>;
  }

  const author = app.author?.github ? (
    <a href={githubUrl(app.author.github)} target="_blank" rel="noopener noreferrer" className="mp-author-link">
      {app.author.name}
    </a>
  ) : (
    <span>{app.author?.name || "Unknown developer"}</span>
  );
  const remix = remixOf(app, snap.catalog);
  const remixLink = remix ? (
    <a
      href={appDetailHref(remix.slug)}
      className="mp-author-link"
      onClick={plainClick(() => openDetail(remix.slug))}
    >
      {remix.name}
    </a>
  ) : null;

  const released = releaseDate(app.last_release_at ?? app.updated_at);
  const remixes = app.remix_count ?? 0;
  const people = app.contributor_count ?? 0;
  const installs = app.install_count ?? 0;
  const strip: InfoStripItem[] = [
    { label: "Category", value: <CategoryGlyph id={app.category} size={18} />, caption: cat },
    { label: "Version", value: app.version, caption: app.installed?.version && app.installed.version !== app.version ? `You have ${app.installed.version}` : "Latest" },
    { label: "Released", value: <span className="mp-strip-dev">{released ?? "—"}</span>, caption: "Reviewed by Fused" },
    { label: "Installs", value: compactCount(installs), caption: installs === 1 ? "Install" : "Installs", title: `${installs} installs` },
    { label: "Remixes", value: String(remixes), caption: remix ? `Remix of ${remix.name}` : remixes === 1 ? "Remix" : "Remixes" },
  ];
  // The row's releases (the catalog's, else the detail's when it sends the
  // stage-B shape); an older server's package releases are not listed.
  const releases: MarketRelease[] = (app.releases?.length ? app.releases : detail?.releases ?? []).filter(
    (r): r is MarketRelease => !!r && typeof (r as MarketRelease).commit === "string",
  );

  return (
    <DetailLayout
      back={back}
      topRight={
        sourceUrl ? (
          <a className="as-toplink" href={sourceUrl} target="_blank" rel="noopener noreferrer">
            Source <ArrowUpRight size={13} strokeWidth={2.2} aria-hidden="true" />
          </a>
        ) : undefined
      }
    >
      <DetailHero
        icon={<AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={112} raised />}
        name={
          <>
            {app.name}
            {verified && <VerifiedMark />}
          </>
        }
        subtitle={app.description}
        author={
          <>
            by {author}
            {remixLink && <> · Remix of {remixLink}</>}
          </>
        }
        tags={app.tags.filter((t) => t !== app.category)}
        actions={<AppActionPill app={app} size="lg" emphasis="filled" />}
        actionsNote={note}
        menu={menu}
        menuLabel={`More actions for ${app.name}`}
      />
      <InfoStrip items={strip} />
      <PreviewStage src={app.preview_url} alt={`${app.name} screenshot`} />
      <DetailBody
        main={
          <>
            <OverviewTabs
              readme={detail ? detail.readme || "" : undefined}
              baseUrl={detail?.readme_base}
              files={detail?.files}
              fileCount={app.file_count}
            />
            {releases.length > 0 && <ReleaseList releases={releases} current={app.installed?.commit ?? null} />}
          </>
        }
        aside={
          <>
            <InfoList
              rows={[
                { label: "Version", value: app.version },
                ...(released ? [{ label: "Released", value: released }] : []),
                ...(remixLink ? [{ label: "Remix of", value: remixLink }] : []),
                { label: "Category", value: cat },
                {
                  label: "Developer",
                  value: author,
                },
                { label: "Size", value: app.size_bytes ? formatSize(app.size_bytes).replace(/\s/, NNBSP) : "—" },
                { label: "Files", value: String(detail?.files.length ?? app.file_count) },
                { label: "Contributors", value: people > 1 ? `${people} credited` : "The author" },
                { label: "Python", value: app.requires_python ? "Required (runs locally)" : "Not required" },
                ...(app.installed
                  ? [
                      {
                        label: "Location",
                        value: (
                          <button type="button" className="as-link-btn mp-path" title={app.installed.path} onClick={() => showInstalledFolder(app.installed!.path)}>
                            {app.installed.path.split("/").slice(-2).join("/")}
                          </button>
                        ),
                      },
                    ]
                  : []),
                ...(sourceUrl
                  ? [
                      {
                        label: "Source",
                        value: (
                          <a href={sourceUrl} target="_blank" rel="noopener noreferrer">
                            GitHub <ArrowUpRight size={12} strokeWidth={2.2} aria-hidden="true" />
                          </a>
                        ),
                      },
                    ]
                  : []),
              ]}
            />
            <TagsSection tags={app.tags} />
            <TrustCard items={trustItemsFor(app)} />
          </>
        }
      />
      {more.length > 0 && !bare && (
        <DetailSection
          title={`More in ${cat}`}
          action={
            <a
              className="mp-see-all"
              href={`${MARKETPLACE_HOME}?category=${encodeURIComponent(app.category)}`}
              onClick={plainClick(() => navigateUrl(`${MARKETPLACE_HOME}?category=${encodeURIComponent(app.category)}`))}
            >
              See all
            </a>
          }
        >
          <div className="mp-grid is-compact">
            {more.map((a) => (
              <MarketCard key={a.slug} app={a} catalog={snap.catalog} variant="compact" />
            ))}
          </div>
        </DetailSection>
      )}
    </DetailLayout>
  );
}

/** Every release of the row, newest first: its date and version, Fused's
 *  What's new, and a "Blocked" tag where Fused blocked it since. The one
 *  this machine runs says so. */
function ReleaseList({ releases, current }: { releases: MarketRelease[]; current: string | null }) {
  return (
    <section className="mp-releases" aria-labelledby="mp-releases-title">
      <h2 className="mp-releases-title" id="mp-releases-title">
        Releases
      </h2>
      <ol className="mp-release-list">
        {releases.map((r) => (
          <li key={r.commit} className={"mp-release" + (r.blocked ? " is-blocked" : "")}>
            <div className="mp-release-head">
              <span className="mp-release-when">{releaseDate(r.at) ?? "Release"}</span>
              {r.version && <span className="mp-release-v">v{r.version}</span>}
              {current && r.commit === current && <span className="mp-release-tag is-current">You have this</span>}
              {r.blocked && (
                <span className="mp-release-tag is-blocked">Blocked</span>
              )}
            </div>
            <p className="mp-release-new">{r.whats_new || "No notes for this release yet."}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}
