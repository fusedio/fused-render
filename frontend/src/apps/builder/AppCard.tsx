// App tile shared by Home's "Recent" grid and the /apps hub. Each tile
// carries a tinted monogram (the app's initial) whose hue is picked
// deterministically from the shell's file-icon palette — stable per name, so
// a tile never changes colour across visits, and the hues are the same family
// the listing already paints file icons with.
import type { AppInfo } from "@platform/lib/api";
import { hrefFor, onAppCardClick, openTargetFor } from "@platform/lib/appEntry";

const APP_HUES = [
  "var(--icon-folder)",
  "var(--icon-code)",
  "var(--icon-data)",
  "var(--icon-json)",
  "var(--icon-image)",
  "var(--icon-geo)",
  "var(--icon-db)",
  "var(--icon-media)",
];

export function hueFor(name: string): string {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0;
  return APP_HUES[Math.abs(h) % APP_HUES.length];
}

export function AppCard({ app }: { app: AppInfo }) {
  const title = app.title || app.name;
  // An anchor, not a button, so middle-click / Cmd-click / "Open in new tab"
  // work; the open rule itself lives in appEntry so all three card surfaces
  // share it. The tooltip names what will actually open.
  return (
    <a
      className="home-app"
      href={hrefFor(app)}
      onClick={(e) => onAppCardClick(e, app)}
      title={openTargetFor(app).path}
    >
      <span className="home-app-monogram" aria-hidden="true" style={{ color: hueFor(app.name) }}>
        {title.charAt(0).toUpperCase()}
      </span>
      <span className="home-app-text">
        <span className="home-app-title">{title}</span>
        {/* The folder name only earns a line when the manifest title differs
            from it — "application / application" is noise. */}
        {title !== app.name && <span className="home-app-sub">{app.name}</span>}
      </span>
      <span className="home-app-tag">{app.tag}</span>
    </a>
  );
}
