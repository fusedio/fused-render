// fs-path <-> /view/ URL codec + navigation. UI-free. The vanilla shell
// registered a route() handler here; the React shell instead listens for the
// "fused:navigate" event (useNavEpoch in location.js) — navigate/navigateUrl
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

function notifyNavigate() {
  window.dispatchEvent(new Event(NAV_EVENT));
}

export function fsPathFromLocation() {
  const p = location.pathname;
  if (!p.startsWith(PREFIX)) return null;
  const rest = p.slice(PREFIX.length);
  const decoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(decodeURIComponent)
    .join("/");
  return "/" + decoded;
}

export function urlForFsPath(fsPath, search) {
  const rest = fsPath.replace(/^\/+/, "");
  const encoded = rest
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return PREFIX + encoded + (search || "");
}

export function navigate(fsPath) {
  // Navigating between files/dirs drops old view params (fresh query string).
  history.pushState(null, "", urlForFsPath(fsPath));
  notifyNavigate();
}

export function navigateUrl(url) {
  // Like navigate(), but preserves the full url (incl. query string) — used
  // when opening a bookmark, whose url carries saved view params.
  history.pushState(null, "", url);
  notifyNavigate();
}

export function currentUrl() {
  return location.pathname + location.search;
}
