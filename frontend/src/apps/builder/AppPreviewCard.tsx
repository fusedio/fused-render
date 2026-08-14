// Big preview card for the /apps hub. Its thumbnail has three shapes, in
// precedence order:
//
//   1. `preview.png` at the app folder's root — an AUTHORED still, served as
//      bytes through /api/fs/raw. First because it is the only one the author
//      chose: a live render shows the page in whatever state it comes up in
//      (empty, mid-load, asking for a file), and a screenshot shows the app
//      making its point. It is also by far the cheapest of the three.
//   2. the app itself, live: `entry_html` in a sandboxed iframe at desktop
//      width (1280px) scaled down to fit the card.
//   3. no entry file at all — the Home grid's tinted monogram, so a card is
//      never blank.
//
// The precedence is a FALLBACK CHAIN, not a fixed choice, and it has to be:
// `preview_image` says a file of that name exists and is non-empty, not that it
// decodes. A corrupt or half-written PNG in an <img> renders as nothing, so a
// wrongly-confident step 1 would be a permanently blank card — strictly worse
// than the live render it replaced. An image error drops to step 2.
//
// Display-only either way: a pointer-events shield keeps every click on the
// card, which opens the app. Both the iframe and the image are lazy, so a big
// workspace doesn't load everything at once.
import { useState } from "react";
import type { AppInfo } from "@platform/lib/api";
import { rawUrl } from "@platform/lib/api";
import { appRecency, hrefFor, onAppCardClick, openTargetFor } from "@platform/lib/appEntry";
import { hueFor } from "@apps/builder/AppCard";

import { timeAgo } from "@platform/lib/format";

// The iframe renders at a fixed desktop width and is scaled to the card by a
// pure-CSS trick: 400% width/height + scale(0.25) means the visual size is
// exactly the .app-pcard-thumb box, whatever the grid column resolves to.
const PREVIEW_SCALE = 0.25;

export function AppPreviewCard({
  app,
  onContextMenu,
  badge,
}: {
  app: AppInfo;
  // Right-click: the card only forwards the event and its own app — the menu
  // state lives one level up (Apps.tsx), so the whole grid shares one portal.
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
  // Extra word in the meta row (e.g. "cloned" on a showcase app the user has
  // copied into Fused/local). Decoration only — the card behaves the same.
  badge?: string;
}) {
  const title = app.title || app.name;
  // The same timestamp the grid SORTS by (last opened, modified standing in) —
  // a card ranked first for being opened just now must not label itself with a
  // stale modified time. appRecency's 0-for-neither is falsy, so timeAgo still
  // returns null and the label hides.
  const ago = timeAgo(appRecency(app));
  // Set when the authored thumbnail fails to decode — see the fallback chain in
  // the module comment. One-way: a retry would loop on a file that is broken.
  const [shotFailed, setShotFailed] = useState(false);
  // An anchor, not a button — see AppCard. The href is what makes middle-click
  // and "Open in new tab" land on the same place a left click does.
  return (
    <a
      className="app-pcard"
      href={hrefFor(app)}
      onClick={(e) => onAppCardClick(e, app)}
      // On the <a>, not on the body: the thumbnail's pointer-events shield sits
      // INSIDE this element, so a right-click over the preview bubbles up here
      // (the iframe itself never sees it) and one handler covers the whole card.
      onContextMenu={onContextMenu && ((e) => onContextMenu(e, app))}
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
      <span className="app-pcard-thumb" aria-hidden="true">
        {app.preview_image && !shotFailed ? (
          <>
            <img
              className="app-pcard-shot"
              src={rawUrl(app.preview_image)}
              alt=""
              loading="lazy"
              onError={() => setShotFailed(true)}
            />
            {/* The same shield the iframe gets. An <img> swallows no clicks of
                its own, but it DOES carry the browser's native drag-the-image
                gesture, which starts a drag on the card instead of the click
                that opens it. */}
            <span className="app-pcard-shield" />
          </>
        ) : app.entry_html ? (
          <>
            <iframe
              src={`/render?path=${encodeURIComponent(app.entry_html)}`}
              style={{
                width: `${100 / PREVIEW_SCALE}%`,
                height: `${100 / PREVIEW_SCALE}%`,
                transform: `scale(${PREVIEW_SCALE})`,
              }}
              loading="lazy"
              tabIndex={-1}
              scrolling="no"
              title=""
            />
            {/* Shield: the preview is display-only — every pointer event lands
                on the card's link, never inside the app — which is also what
                keeps middle-click over the preview a new tab for the app. */}
            <span className="app-pcard-shield" />
          </>
        ) : (
          <span className="app-pcard-monogram" style={{ color: hueFor(app.name) }}>
            {title.charAt(0).toUpperCase()}
          </span>
        )}
      </span>
    </a>
  );
}
