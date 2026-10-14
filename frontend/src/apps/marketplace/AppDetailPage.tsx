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
  plainClick,
  type InfoStripItem,
} from "@platform/ui/appstore";
import type { MenuEntry } from "@platform/ui/ContextMenu";
import { formatSize } from "@platform/lib/format";
import { navigateUrl } from "@platform/lib/router";
import { openUninstallApp } from "@platform/lib/uninstall-app";
import {
  MARKETPLACE_HOME,
  actionFor,
  appPageHref,
  categoryLabel,
  showInstalledFolder,
  useAppDetail,
  useJob,
  type AppEntry,
  type CatalogSnapshot,
} from "@platform/lib/marketplace";
import { AppActionPill, CategoryGlyph, EmptyState, MarketCard } from "./shared";
import { trustItemsFor } from "./trust";

function githubUrl(handle: string): string {
  return `https://github.com/${encodeURIComponent(handle.replace(/^@/, ""))}`;
}

export function AppDetailPage({ slug, snap }: { slug: string; snap: CatalogSnapshot }) {
  const { detail, error, reload } = useAppDetail(slug);
  const fromCatalog = snap.catalog?.apps.find((a) => a.slug === slug) ?? null;
  const app: AppEntry | null = detail?.app ?? fromCatalog;
  const job = useJob(slug);

  const back = {
    label: "Marketplace",
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
                ? `There is no app called “${slug}” in the Marketplace.`
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
  if (app.source_url) {
    const src = app.source_url;
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

  let note: ReactNode = null;
  if (action.kind === "update") {
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

  const strip: InfoStripItem[] = [
    { label: "Category", value: <CategoryGlyph id={app.category} size={18} />, caption: cat },
    { label: "Version", value: app.version, caption: app.installed?.version && app.installed.version !== app.version ? `You have ${app.installed.version}` : "Latest" },
    { label: "Developer", value: <span className="mp-strip-dev">{app.author?.name || "—"}</span>, caption: app.author?.github ? "@" + app.author.github.replace(/^@/, "") : "Community" },
    { label: "Size", value: app.size_bytes ? formatSize(app.size_bytes).replace(/\s/, " ") : "—", caption: "Download" },
    { label: "Python", value: app.requires_python ? "Required" : "Not required", caption: app.requires_python ? "Runs locally" : "Page only" },
    { label: "Files", value: String(detail?.files.length ?? app.file_count), caption: "In folder" },
  ];

  return (
    <DetailLayout
      back={back}
      topRight={
        app.source_url ? (
          <a className="as-toplink" href={app.source_url} target="_blank" rel="noopener noreferrer">
            Source <ArrowUpRight size={13} strokeWidth={2.2} aria-hidden="true" />
          </a>
        ) : undefined
      }
    >
      <DetailHero
        icon={<AppIcon src={app.icon_url} name={app.name} seed={app.slug} size={112} raised />}
        name={app.name}
        subtitle={app.description}
        author={<>by {author}</>}
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
          </>
        }
        aside={
          <>
            <InfoList
              rows={[
                { label: "Version", value: app.version },
                { label: "Category", value: cat },
                {
                  label: "Developer",
                  value: author,
                },
                { label: "Size", value: app.size_bytes ? formatSize(app.size_bytes) : "—" },
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
                ...(app.source_url
                  ? [
                      {
                        label: "Source",
                        value: (
                          <a href={app.source_url} target="_blank" rel="noopener noreferrer">
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
      {more.length > 0 && (
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
