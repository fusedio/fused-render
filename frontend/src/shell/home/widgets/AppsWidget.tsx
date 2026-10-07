import { appIconUrl } from "@platform/lib/api";
import { isRasterIconUrl, useThemedIconSrc } from "@platform/lib/app-icon-src";
import { hrefFor, isBrowserHandledClick, openApp } from "@platform/lib/appEntry";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import { AppStar } from "@platform/ui/AppStar";
import type { AppInfo } from "@platform/lib/api";
import { CardStrip } from "../CardStrip";
import { SkeletonRow } from "../skeleton";
import { MAX_ROW, softNavigate, useStripCount } from "../strip";
import { useHomeApps } from "../data";
import { dims, itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ListSkeleton } from "./bits";

/** An app's icon in the shared tile square (the brand's AppStar when it has none — the same mark the Apps page's cards and the sidebar fall back to). */
export function AppGlyph({ app }: { app: AppInfo }) {
  const iconUrl = app.icon ? appIconUrl(app.icon, app.icon_mtime) : null;
  const src = useThemedIconSrc(iconUrl);
  return (
    <span className="hw-tile-icon" aria-hidden="true">
      {src ? <img src={src} alt="" className={isRasterIconUrl(iconUrl) ? "is-raster" : undefined} /> : <AppStar className="hw-tile-star" />}
    </span>
  );
}

function AppTile({ app }: { app: AppInfo }) {
  const name = app.title || app.name;
  return (
    <a
      className="hw-tile"
      href={hrefFor(app)}
      title={name}
      onClick={(e) => {
        if (e.defaultPrevented || isBrowserHandledClick(e)) return;
        e.preventDefault();
        openApp(app);
      }}
    >
      <AppGlyph app={app} />
      <span className="hw-tile-name">{name}</span>
    </a>
  );
}

export function AppsWidget({ widget }: { widget: Widget }) {
  const { rows } = dims(widget.size);
  const cards = widget.format === "cards";
  const { ref, count, limit } = useStripCount();
  const cap = cards ? (count ?? 0) * rows : itemCapacity(widget.size, widget.format);
  // Cards ask for what the measured row can draw (the server's recents-first
  // fast path depends on it); icon tiles ask for their fixed capacity.
  const { apps, appsError, retry } = useHomeApps(cards ? limit : Math.min(cap * 2, MAX_ROW), cards ? rows : 1);
  return (
    <div ref={ref} className="hw-body">
      {apps === null ? (
        cards ? (
          <SkeletonRow count={cap} label="Loading apps" variant="app" />
        ) : (
          <ListSkeleton rows={2} label="Loading apps" />
        )
      ) : apps.length ? (
        cards ? (
          <CardStrip count={count} rows={rows} total={Math.min(apps.length, MAX_ROW)}>
            {apps.slice(0, MAX_ROW).map((app) => (
              <AppPreviewCard key={app.path} app={app} />
            ))}
          </CardStrip>
        ) : (
          <CardStrip variant="icons" count={count} rows={rows} total={apps.length}>
            {apps.slice(0, MAX_ROW).map((app) => (
              <AppTile key={app.path} app={app} />
            ))}
            {apps.length >= MAX_ROW && (
              <a className="hw-tile is-more" href="/apps" onClick={(e) => softNavigate(e, "/apps")} aria-label="All apps">
                <span className="hw-tile-icon" aria-hidden="true">
                  <span className="hw-tile-glyph">›</span>
                </span>
                <span className="hw-tile-name">All apps</span>
              </a>
            )}
          </CardStrip>
        )
      ) : appsError ? (
        <ErrorLine message={appsError} onRetry={retry} />
      ) : (
        <EmptyLine>No apps yet. Build one and it'll show up here.</EmptyLine>
      )}
    </div>
  );
}
