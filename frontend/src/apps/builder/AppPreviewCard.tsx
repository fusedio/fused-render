// Big preview card for the /apps hub. The thumbnail is the app itself: its
// entry HTML rendered live in a sandboxed iframe at desktop width (1280px)
// and scaled down to fit the card — display only, a pointer-events shield
// keeps every click on the card, which opens the app. Apps without an entry
// file fall back to the Home grid's tinted monogram so the card is never
// blank. Iframes are lazy so a big workspace doesn't render everything at
// once.
import type { AppInfo } from "@platform/lib/api";
import { navigate } from "@platform/lib/router";
import { hueFor } from "@apps/builder/AppCard";

// "3d ago" style stamp for the card meta line; null when the backend didn't
// report a modified time.
export function timeAgo(epochSeconds: number | null | undefined): string | null {
  if (!epochSeconds) return null;
  const s = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return `${d}d ago`;
  const mo = Math.floor(d / 30);
  if (mo < 12) return `${mo}mo ago`;
  return `${Math.floor(mo / 12)}y ago`;
}

// The iframe renders at a fixed desktop width and is scaled to the card by a
// pure-CSS trick: 400% width/height + scale(0.25) means the visual size is
// exactly the .app-pcard-thumb box, whatever the grid column resolves to.
const PREVIEW_SCALE = 0.25;

export function AppPreviewCard({ app }: { app: AppInfo }) {
  const open = () => {
    // Folder-first: an app with an entry opens in the claude_split view (app
    // beside a Claude chat); without one, the plain folder listing.
    if (app.entry_html) navigate(app.path, { isDir: true, mode: "claude_split" });
    else navigate(app.path, { isDir: true });
  };
  const title = app.title || app.name;
  const ago = timeAgo(app.updated_at);
  return (
    <button type="button" className="app-pcard" onClick={open} title={app.path}>
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
                on the card button, never inside the app. */}
            <span className="app-pcard-shield" />
          </>
        ) : (
          <span className="app-pcard-monogram" style={{ color: hueFor(app.name) }}>
            {title.charAt(0).toUpperCase()}
          </span>
        )}
      </span>
      <span className="app-pcard-body">
        <span className="app-pcard-title">{title}</span>
        <span className="app-pcard-meta">
          <span className="app-pcard-tag">{app.tag}</span>
          {title !== app.name && <span className="app-pcard-name">{app.name}</span>}
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
    </button>
  );
}
