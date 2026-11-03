// URL forms the path bar (Ctrl/Cmd+L, Breadcrumb) accepts besides a plain
// local path: file:// only — a pure client-side rewrite to the local path it
// names. No server round trip: the explorer already browses the whole
// filesystem, so a file URL is just a spelling of a path the user could have
// typed.
//
// Anything else (http://, ftp://, …) is rejected by name rather than silently
// treated as a path — "https:/home" is not a folder anyone meant to open.

// A scheme prefix, per RFC 3986's scheme grammar plus the "//" authority
// marker. Requiring "//" is what keeps a Windows path ("C:\Users") and a
// relative path with a colon out of this branch entirely.
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
  // "file://server/share" is a remote UNC location, not a local path — the
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
  // A bare drive ("file:///C:") is the drive root.
  if (/^\/[A-Za-z]:$/.test(path)) return path.slice(1) + "/";
  // A Windows file URL roots the drive under the authority's slash
  // ("file:///C:/Users/x") — drop it; on POSIX the leading slash IS the root.
  return dropFileUrlDriveSlash(path);
}

// "/C:/x" (the authority's slash still in front of a drive letter) -> "C:/x".
// A separator after the colon is required, so a POSIX "/a:notes" stays rooted
// and a bare "/C:" is left alone (it is not a drive root here).
export function dropFileUrlDriveSlash(path: string): string {
  return /^\/[A-Za-z]:[\\/]/.test(path) ? path.slice(1) : path;
}

// A Windows drive-letter absolute path ("C:\x" or "C:/x"). The ONE definition
// of that shape for the frontend: a backslash is a legal POSIX filename
// character, so anything that rewrites backslashes must gate on this first.
export const DRIVE_ABS = /^[A-Za-z]:[\\/]/;

export function isDriveAbs(p: string): boolean {
  return DRIVE_ABS.test(p);
}

// The shell's canonical forward-slash spelling of a path: backslashes become
// "/" ONLY for a drive-letter path; every other string is returned untouched.
export function canonFsPath(p: string): string {
  const s = String(p);
  return DRIVE_ABS.test(s) ? s.replace(/\\/g, "/") : s;
}

// Windows fs paths are rooted at a drive letter ("C:/..."), not at "/": the
// shell's canonical form adds a leading slash only for POSIX paths. A bare
// drive ("C:", how a drive root decodes from a URL whose segment split drops
// the trailing slash) becomes "C:/" -- bare "C:" is cwd-relative.
export function rootedFsPath(joined: string): string {
  if (/^[A-Za-z]:$/.test(joined)) return joined + "/";
  return /^[A-Za-z]:\//.test(joined) ? joined : "/" + joined;
}

// A fs path as URL path segments (no leading slash). Only a drive-letter path
// has its backslashes folded -- on POSIX a backslash is a legal filename
// character and round-trips untouched.
export function encodeFsPathSegments(fsPath: string): string {
  return canonFsPath(fsPath)
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
}

// The folder a path lives in. A drive path's parent never drops below its
// root ("C:/x" -> "C:/", never the cwd-relative "C:"); POSIX bottoms out at
// "/"; a UNC path bottoms out at its "\\server\share". Backslashes split only
// drive and UNC paths (canonFsPath), so "/tmp/a\\b" -> "/tmp".
export function parentFsPath(p: string): string {
  const s = canonFsPath(p);
  if (s.startsWith("\\\\")) {
    const parts = s.slice(2).split(/[\\/]+/).filter(Boolean);
    return "\\\\" + parts.slice(0, Math.max(2, parts.length - 1)).join("\\");
  }
  const drive = /^[A-Za-z]:/.test(s) && isDriveAbs(s) ? s.slice(0, 2) : "";
  const body = s.slice(drive.length).replace(/\/+$/, "");
  const i = body.lastIndexOf("/");
  return drive + (i <= 0 ? "/" : body.slice(0, i));
}
