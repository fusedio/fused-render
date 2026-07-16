// fs-path <-> /view/ URL codec + navigation. UI-free. The vanilla shell
// registered a route() handler here; the React shell instead listens for the
// "fused:navigate" event (useNavEpoch in lib/hooks.ts) — navigate/navigateUrl
// dispatch it after pushState, popstate is subscribed alongside it.
export const VIEW_PREFIX = "/view/";

// Embed = chrome-free variant of view (same shell, same routing, just no
// sidebar/breadcrumb/preview-header). The mode is fixed at page load: both
// prefixes are served by full page loads, so it can't change without one.
export const EMBED_PREFIX = "/embed/";
export const IS_EMBED =
  location.pathname.startsWith(EMBED_PREFIX) || location.pathname === "/embed";
// URL prefix for this page's mode. Keeps refresh, in-listing navigation, and
// param sync (iframe runtime's history.replaceState) inside the active prefix.
const PREFIX = IS_EMBED ? EMBED_PREFIX : VIEW_PREFIX;

export const NAV_EVENT = "fused:navigate";

function notifyNavigate(): void {
  window.dispatchEvent(new Event(NAV_EVENT));
}

// Windows fs paths are rooted at a drive letter ("C:/…"), not at "/" — the
// shell's canonical form keeps forward slashes and adds a leading slash only
// for POSIX paths. A bare drive ("C:", how a drive root decodes from a URL,
// whose segment split drops the trailing slash) canonicalizes to "C:/" —
// bare "C:" is cwd-relative for os.stat on Windows.
export function rootedFsPath(joined: string): string {
  if (joined.startsWith("\\\\")) return joined;
  if (/^[A-Za-z]:$/.test(joined)) return joined + "/";
  return /^[A-Za-z]:\//.test(joined) ? joined : "/" + joined;
}

export function fsPathFromLocation(): string | null {
  const p = location.pathname;
  if (!p.startsWith(PREFIX)) return null;
  const rest = p.slice(PREFIX.length);
  const decoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(decodeURIComponent)
    .join("/");
  return rootedFsPath(decoded);
}

export function urlForFsPath(fsPath: string, search?: string): string {
  // Windows callers (server stat/list results, bookmarks) may carry
  // backslashes; the URL codec speaks forward slashes only. Normalize ONLY
  // drive-letter paths — on POSIX a backslash is a legal filename character
  // and must round-trip untouched.
  const norm = /^[A-Za-z]:[\\/]/.test(fsPath) ? fsPath.replace(/\\/g, "/") : fsPath;
  const rest = norm.replace(/^\/+/, "");
  const encoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return PREFIX + encoded + (search || "");
}

export function navigate(fsPath: string): void {
  // Navigating between files/dirs drops old view params (fresh query string).
  history.pushState(null, "", urlForFsPath(fsPath));
  notifyNavigate();
}

export function navigateUrl(url: string): void {
  // Like navigate(), but preserves the full url (incl. query string) — used
  // when opening a bookmark, whose url carries saved view params.
  history.pushState(null, "", url);
  notifyNavigate();
}

export function currentUrl(): string {
  return location.pathname + location.search;
}
