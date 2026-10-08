// Ordering, folder line and filter for the add sheet's App picker.
import type { AppInfo } from "@platform/lib/api";
import { sortApps } from "@platform/lib/appEntry";

const title = (a: AppInfo) => a.title || a.name;

/** The folder an app lives in (the parent of its folder, or of its `.fused`
    file), with the home directory shown as "~". */
export function appFolderLine(app: AppInfo, home?: string): string {
  const p = app.path.replace(/\\/g, "/").replace(/\/+$/, "");
  const i = p.lastIndexOf("/");
  const dir = i > 0 ? p.slice(0, i) : i === 0 ? "/" : "";
  const h = home ? home.replace(/\\/g, "/").replace(/\/+$/, "") : "";
  if (h && dir === h) return "~";
  if (h && dir.startsWith(h + "/")) return "~" + dir.slice(h.length);
  const m = /^\/(?:Users|home)\/[^/]+(\/.*)?$/.exec(dir);
  return m ? "~" + (m[1] ?? "") : dir;
}

/** The picker's list in the shared /apps order. */
export function pickerApps(apps: AppInfo[]): AppInfo[] {
  return sortApps(apps);
}

export function filterPickerApps(apps: AppInfo[], query: string, home?: string): AppInfo[] {
  const q = query.trim().toLowerCase();
  if (!q) return apps;
  return apps.filter(
    (a) =>
      title(a).toLowerCase().includes(q) ||
      a.name.toLowerCase().includes(q) ||
      appFolderLine(a, home).toLowerCase().includes(q),
  );
}

export function isWebUrl(s: string): boolean {
  return /^https?:\/\/\S+$/i.test(s);
}

/** A typed address as a full http(s) URL: kept as is, a bare host gets https://, anything else is null. */
export function normalizeWebUrl(input: string): string | null {
  const t = input.trim();
  if (isWebUrl(t)) return t;
  if (/^[\w.-]+\.[a-z]{2,}(\/\S*)?$/i.test(t)) return "https://" + t;
  return null;
}

/** The last path segment (either separator, trailing slashes ignored); the full path when there is none.
    A web URL titles as its hostname (minus a leading www.) instead. */
export function pageTitle(path: string): string {
  if (isWebUrl(path)) {
    try {
      return new URL(path).hostname.replace(/^www\./, "");
    } catch {
      // fall through to the segment logic
    }
  }
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return parts[parts.length - 1] ?? path;
}
