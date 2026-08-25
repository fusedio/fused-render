// Big preview card for Home's "Fused Apps" row (`.app-pcard`, apps.css). The
// thumbnail — an authored preview.png, or the app itself live in a scaled
// iframe, or an empty box — is AppThumb, which owns the fallback chain and the
// lazy iframe scheduling; this file is the card around it: the link that opens
// the app, the title row, and the hover-revealed export chip. The /apps hub
// draws its own card on the same thumb (apps/builder/AppsCard.tsx).
import { useState } from "react";
import type { AppInfo } from "@platform/lib/api";
import { exportAppFile } from "@platform/lib/appShot";
import { pushToast } from "@platform/lib/toast";
import { MenuIcons } from "@platform/ui/MenuIcons";
import { AppThumb } from "@platform/ui/AppThumb";
import { navigateUrl } from "@platform/lib/router";
import {
  appRecency,
  hrefFor,
  isBrowserHandledClick,
  onAppCardClick,
  openTargetFor,
} from "@platform/lib/appEntry";

import { timeAgo } from "@platform/lib/format";

export function AppPreviewCard({
  app,
  onContextMenu,
  badge,
  href,
}: {
  app: AppInfo;
  // Right-click handler — a hub opens a context menu; Home passes none.
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
  // Extra meta pill (a "cloned" marker on showcase cards).
  badge?: string;
  // Override the card's link target — the default opens the app (hrefFor).
  href?: string;
}) {
  const title = app.title || app.name;
  // The same timestamp the grid SORTS by (last opened, modified standing in) —
  // a card ranked first for being opened just now must not label itself with a
  // stale modified time. appRecency's 0-for-neither is falsy, so timeAgo still
  // returns null and the label hides.
  const ago = timeAgo(appRecency(app));
  const [hovered, setHovered] = useState(false);
  // The thumb element once its body iframe has painted the app — the export
  // chip's crop source (appShot). null until then, so the export stages the
  // app instead of cropping an empty box.
  const [liveThumb, setLiveThumb] = useState<HTMLSpanElement | null>(null);
  // An anchor, not a button — see AppCard. The href is what makes middle-click
  // and "Open in new tab" land on the same place a left click does.
  return (
    <a
      className="app-pcard"
      href={href ?? hrefFor(app)}
      onClick={(e) => {
        if (!href) return onAppCardClick(e, app);
        if (e.defaultPrevented || isBrowserHandledClick(e)) return;
        e.preventDefault();
        navigateUrl(href, { isDir: false });
      }}
      onContextMenu={onContextMenu && ((e) => onContextMenu(e, app))}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={openTargetFor(app).path}
    >
      <span className="app-pcard-body">
        <span className="app-pcard-title">{title}</span>
        <span className="app-pcard-meta">
          <span className="app-pcard-tag">{app.tag}</span>
          {title !== app.name && <span className="app-pcard-name">{app.name}</span>}
          {badge && <span className="app-pcard-name">{badge}</span>}
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
      <AppThumb app={app} hovered={hovered} onBodyLive={setLiveThumb} />
      {/* Hover-revealed export (SPEC §43 AF-4, D391): the same action as the
          right-click menu's "Export App File", surfaced so it is one visible
          click. A SIBLING of the thumb, not a child: the thumb span is
          aria-hidden (it is decoration), and a focusable button inside an
          aria-hidden subtree is announced as nothing by assistive tech while
          still taking tab focus. Positioned over the thumb via the card's own
          positioning context. A <button> inside the card's <a>: it must both
          preventDefault (or the card link opens the app) and stopPropagation
          (or the click ALSO bubbles to onAppCardClick). Not rendered on an
          exported .fused card (kind "appfile", D396): its path is the file
          itself and the export route only takes app folders. */}
      {app.kind !== "appfile" && (
      <button
        type="button"
        className="app-pcard-export"
        title={"Export " + (app.title || app.name) + " as a .fused app file"}
        aria-label="Export app file"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          // Also captures a tab screenshot into the file's preview.png when
          // the folder has no authored one (appShot, D396). The thumb element
          // rides along as the crop source: a card without a preview.png is
          // already showing the live app there, so nothing has to flash —
          // but ONLY once that frame has loaded (`bodyLive`). Two card
          // previews start at a time, so an unstarted card's thumb is an
          // empty box, and cropping it would bake the empty box in as the
          // artifact's permanent thumbnail. Offer nothing instead and
          // appShot stages the app full-screen for the shot.
          exportAppFile(app, liveThumb).catch((err: Error) =>
            pushToast({
              msg: "Could not export " + app.name + ": " + err.message,
              tone: "error",
            }),
          );
        }}
      >
        {MenuIcons.download}
      </button>
      )}
    </a>
  );
}
