// Big preview card for the /apps hub. The thumbnail is the app itself: its
// entry HTML rendered live in a sandboxed iframe at desktop width (1280px)
// and scaled down to fit the card — display only, a pointer-events shield
// keeps every click on the card, which opens the app. Apps without an entry
// file fall back to the Home grid's tinted monogram so the card is never
// blank. Iframes are lazy so a big workspace doesn't render everything at
// once.
import type { AppInfo } from "@platform/lib/api";
import { hrefFor, onAppCardClick, openTargetFor } from "@platform/lib/appEntry";
import { hueFor } from "@apps/builder/AppCard";

import { timeAgo } from "@platform/lib/format";

// The iframe renders at a fixed desktop width and is scaled to the card by a
// pure-CSS trick: 400% width/height + scale(0.25) means the visual size is
// exactly the .app-pcard-thumb box, whatever the grid column resolves to.
const PREVIEW_SCALE = 0.25;

export function AppPreviewCard({
  app,
  onContextMenu,
}: {
  app: AppInfo;
  // Right-click: the card only forwards the event and its own app — the menu
  // state lives one level up (Apps.tsx), so the whole grid shares one portal.
  onContextMenu?: (e: React.MouseEvent, app: AppInfo) => void;
}) {
  const title = app.title || app.name;
  const ago = timeAgo(app.updated_at);
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
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
      <span className="app-pcard-thumb" aria-hidden="true">
        {app.entry_html ? (
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
