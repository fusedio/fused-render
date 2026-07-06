// fs-path <-> /view/ URL codec + navigation. UI-free: the actual route
// handler is registered by main.js, so every module can import navigate()
// without creating import cycles.
export const VIEW_PREFIX = "/view/";

let routeHandler = () => {};

export function setRouteHandler(fn) {
  routeHandler = fn;
}

export function fsPathFromLocation() {
  const p = location.pathname;
  if (!p.startsWith(VIEW_PREFIX)) return null;
  const rest = p.slice(VIEW_PREFIX.length);
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
  return VIEW_PREFIX + encoded + (search || "");
}

export function navigate(fsPath) {
  // Navigating between files/dirs drops old view params (fresh query string).
  history.pushState(null, "", urlForFsPath(fsPath));
  routeHandler();
}

export function navigateUrl(url) {
  // Like navigate(), but preserves the full url (incl. query string) — used
  // when opening a bookmark, whose url carries saved view params.
  history.pushState(null, "", url);
  routeHandler();
}

export function currentUrl() {
  return location.pathname + location.search;
}

window.addEventListener("popstate", () => routeHandler());
