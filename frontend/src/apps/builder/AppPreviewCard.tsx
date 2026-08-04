// Big preview card for the /apps hub. The thumbnail is the app itself, by
// whichever of three routes its entry needs:
//
//   * a PAGE — rendered live in a sandboxed iframe at desktop width (1280px)
//     and scaled down to fit the card. Display only: a pointer-events shield
//     keeps every click on the card, which opens the app. Iframes are lazy so
//     a big workspace doesn't render everything at once.
//   * an IMAGE — the bytes straight from /api/fs/raw. A Claude Science figure
//     artifact is a real PNG, and /render is HTML-only (it decodes the file as
//     UTF-8 text), so the iframe route cannot serve one.
//   * anything else (a .csv table, say) — the tinted monogram, labelled with
//     the entry's extension, rather than booting a whole template per card in
//     a grid of them.
//
// Apps with no entry at all get the monogram too (their initial, there being
// no extension to name), so a card is never blank.
import { appSourceLabel, type AppInfo } from "@platform/lib/api";
import { entryOf, extLabel, isImageEntry, openApp, rawUrl } from "@platform/lib/appEntry";
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
  const title = app.title || app.name;
  const ago = timeAgo(app.updated_at);
  const sourceLabel = appSourceLabel(app.source);
  const entry = entryOf(app);
  const ext = extLabel(entry);
  return (
    <button
      type="button"
      className="app-pcard"
      onClick={() => openApp(app)}
      title={entry ?? app.path}
    >
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
        ) : isImageEntry(entry) ? (
          <img className="app-pcard-image" src={rawUrl(entry as string)} loading="lazy" alt="" />
        ) : (
          // The extension when the entry has one — "CSV" tells you what the
          // card holds; the initial of its filename doesn't, and a grid of
          // tables all reading "O" tells you nothing at all.
          <span
            className={`app-pcard-monogram${ext ? " is-ext" : ""}`}
            style={{ color: hueFor(app.name) }}
          >
            {ext ?? title.charAt(0).toUpperCase()}
          </span>
        )}
      </span>
      <span className="app-pcard-body">
        <span className="app-pcard-title">{title}</span>
        <span className="app-pcard-meta">
          {/* Provenance, because the tag says nothing about where an app came
              from: for a Claude Science artifact it is the project's name, and
              for a discovered folder it is just a tag that another workspace
              also happens to use. Absent for a workspace app — the default
              needs no label. */}
          {sourceLabel && <span className="app-source">{sourceLabel}</span>}
          <span className="app-pcard-tag">{app.tag}</span>
          {title !== app.name && <span className="app-pcard-name">{app.name}</span>}
          {ago && <span className="app-pcard-ago">{ago}</span>}
        </span>
      </span>
    </button>
  );
}
