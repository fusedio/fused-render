import { appIconUrl } from "@platform/lib/api";
import { useThemedIconSrc } from "@platform/lib/app-icon-src";
import { hrefFor, isBrowserHandledClick, openApp } from "@platform/lib/appEntry";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import type { AppInfo } from "@platform/lib/api";
import { SkeletonRow } from "../skeleton";
import { MAX_ROW, useStripCount } from "../strip";
import { useHomeApps } from "../data";
import { dims, itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ListSkeleton } from "./bits";

function AppTile({ app }: { app: AppInfo }) {
  const iconUrl = app.icon ? appIconUrl(app.icon, app.icon_mtime) : null;
  const src = useThemedIconSrc(iconUrl);
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
      <span className="hw-tile-icon" aria-hidden="true">
        {src ? <img src={src} alt="" /> : <span className="hw-tile-glyph">★</span>}
      </span>
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
  const { apps, appsError, retry } = useHomeApps(cards ? limit : Math.min(cap, MAX_ROW), cards ? rows : 1);
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
          <div className="home-row hw-cards">
            {apps.slice(0, cap).map((app) => (
              <AppPreviewCard key={app.path} app={app} />
            ))}
          </div>
        ) : (
          <div className="hw-icons">
            {apps.slice(0, cap).map((app) => (
              <AppTile key={app.path} app={app} />
            ))}
          </div>
        )
      ) : appsError ? (
        <ErrorLine message={appsError} onRetry={retry} />
      ) : (
        <EmptyLine>No apps yet. Build one and it'll show up here.</EmptyLine>
      )}
    </div>
  );
}
