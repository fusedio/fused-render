import { useState } from "react";
import { appIconUrl } from "@platform/lib/api";
import { isRasterIconUrl, useThemedIconSrc } from "@platform/lib/app-icon-src";
import { hrefFor, isBrowserHandledClick, openApp } from "@platform/lib/appEntry";
import { AppPreviewCard } from "@platform/ui/AppPreviewCard";
import type { AppInfo } from "@platform/lib/api";
import { fallbackTile } from "../../../dock/lib";
import { CardStrip } from "../CardStrip";
import { SkeletonRow } from "../skeleton";
import { MAX_ROW, useStripCount } from "../strip";
import { usePagedApps, useHomeApps } from "../data";
import { mergeApps } from "./mergeApps";
import { CELL, dims, itemCapacity, type Widget } from "../layout";
import { EmptyLine, ErrorLine, ListSkeleton } from "./bits";

/** An app's icon as the dock draws it: a squircle tile, the icon cover-filling it. No icon (or one that fails to load) gets the dock's solid palette tile with the app's first letter. */
export function AppGlyph({ app }: { app: AppInfo }) {
  const iconUrl = app.icon ? appIconUrl(app.icon, app.icon_mtime) : null;
  const src = useThemedIconSrc(iconUrl);
  const [failed, setFailed] = useState<string | null>(null);
  if (!src || failed === src) {
    const { color, letter } = fallbackTile(app.title || app.name);
    return (
      <span className="hw-tile-icon is-fallback" style={{ background: color }} aria-hidden="true">
        <span className="hw-tile-mono">{letter}</span>
      </span>
    );
  }
  return (
    <span className="hw-tile-icon" aria-hidden="true">
      <img src={src} alt="" className={isRasterIconUrl(iconUrl) ? "is-raster" : undefined} onError={() => setFailed(src)} />
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
  const rows = dims(widget.size).rows / CELL; // dims() is in half-cell units
  const cards = widget.format === "cards";
  const { ref, count, limit } = useStripCount();
  const cap = cards ? (count ?? 0) : itemCapacity(widget.size, widget.format);
  // Cards ask for what the measured row can draw (the server's recents-first
  // fast path depends on it); icon tiles ask for their fixed capacity.
  const { apps, appsError, retry } = useHomeApps(cards ? limit : Math.min(cap * 2, MAX_ROW), 1, widget.sort ?? "opened");
  const { all, loadMore } = usePagedApps(!cards);
  const merged = cards ? [] : mergeApps((apps ?? []).slice(0, MAX_ROW), all);
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
          <CardStrip count={count} rows={1} total={Math.min(apps.length, MAX_ROW)}>
            {apps.slice(0, MAX_ROW).map((app) => (
              <AppPreviewCard key={app.path} app={app} />
            ))}
          </CardStrip>
        ) : (
          <CardStrip variant="icons" count={count} rows={rows} onNearEnd={loadMore} total={merged.length}>
            {merged.map((app) => (
              <AppTile key={app.path} app={app} />
            ))}
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
