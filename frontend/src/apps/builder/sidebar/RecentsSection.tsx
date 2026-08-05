// The app-builder sidebar's Recents section: recently opened apps, from the
// builder's own store (apps/builder/lib/recents.ts — independent of the
// explorer's file recents). Rows reuse the bookmark row classes so the
// section reads like the explorer's.
import { APP_OPEN_MODE } from "@platform/lib/appEntry";
import { APP_ROUTE_PREFIX, navigateUrl } from "@platform/lib/router";
import { loadAppRecents, useAppRecentsVersion } from "@apps/builder/lib/recents";

export default function RecentsSection() {
  useAppRecentsVersion();
  const recents = loadAppRecents();

  if (recents.length === 0) return null;
  return (
    <div className="sidebar-section sidebar-recents">
      <div className="sidebar-heading">Recents</div>
      {recents.map((r) => {
        const url =
          APP_ROUTE_PREFIX +
          encodeURIComponent(r.tag) +
          "/" +
          encodeURIComponent(r.name) +
          // Opening a recent app IS opening an app: same mode the hub's cards
          // use (appEntry), never the create-a-new-app split view.
          "?_mode=" + APP_OPEN_MODE;
        return (
          <a
            key={r.tag + "/" + r.name}
            className="bookmark-row recent-row"
            href={url}
            title={r.tag + "/" + r.name}
            onClick={(e) => {
              e.preventDefault();
              navigateUrl(url, { isDir: true });
            }}
          >
            <span className="bookmark-glyph recent-glyph" aria-hidden="true">
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
                <circle cx="8" cy="8" r="6.2" />
                <path d="M8 4.8V8l2.3 1.6" />
              </svg>
            </span>
            <span className="bookmark-name">{r.title || r.name}</span>
          </a>
        );
      })}
    </div>
  );
}
