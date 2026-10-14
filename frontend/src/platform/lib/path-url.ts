// URL forms the path bar (Ctrl/Cmd+L, Breadcrumb) accepts besides a plain
// local path: file:// only — a pure client-side rewrite to the local path it
// names. No server round trip: the explorer already browses the whole
// filesystem, so a file URL is just a spelling of a path the user could have
// typed.
//
// Anything else (http://, ftp://, …) is rejected by name rather than silently
// treated as a path — "https:/home" is not a folder anyone meant to open.

// A scheme prefix, per RFC 3986's scheme grammar plus the "//" authority
// marker. Requiring "//" is what keeps a relative path with a colon out of
// this branch entirely.
const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):\/\//;

// The lowercased scheme of a URL-looking string, else null (= treat as a path).
export function urlScheme(value: string): string | null {
  const m = SCHEME_RE.exec(value);
  return m ? m[1].toLowerCase() : null;
}

// file:// URL -> local path. Throws (message is user-facing) for a form that
// names no local file.
export function fileUrlToPath(url: string): string {
  const rest = url.slice("file://".length);
  const slash = rest.indexOf("/");
  const host = slash === -1 ? rest : rest.slice(0, slash);
  // "file://server/share" names another machine, not a local path — the
  // only hosts that mean "this machine" are the empty one and localhost.
  if (host && host.toLowerCase() !== "localhost") {
    throw new Error(`Can't open a file:// URL on another host (${host})`);
  }
  let path = slash === -1 ? "" : rest.slice(slash);
  try {
    path = decodeURIComponent(path);
  } catch {
    throw new Error("That file:// URL is not valid percent-encoding");
  }
  if (!path) throw new Error("That file:// URL names no path");
  return path;
}
