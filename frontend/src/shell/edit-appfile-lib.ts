// The `?_edit_appfile=<payload>` hand-off (SPEC §26 DL-7): the server's
// GET /clone sends a well-formed `fused-render://open?file=` deep link
// straight to the file's install page (AF-14), and a MALFORMED one to Home
// with the raw payload in this param so the shell can report it.
// `FetchAppFileBoot` reads it exactly once at boot and strips it, so a reload
// or Back never re-runs the hand-off. Pure string work here so it is testable
// without a DOM; the name is mirrored in deeplink.py (`EDIT_APPFILE_PARAM`).
export const EDIT_APPFILE_PARAM = "_edit_appfile";
// The `?_fetch_appfile=<http(s) url>` hand-off (DL-8): a
// `fused-render://open?url=` link, redirected into the shell the same way.
// `FetchAppFileBoot` downloads and opens it. Mirrored in deeplink.py
// (`FETCH_APPFILE_PARAM`).
export const FETCH_APPFILE_PARAM = "_fetch_appfile";
// The `?_fetch_appgit=<github repo url>` hand-off (DL-9): a
// `fused-render://open?git=` link; the server builds the repo's one app into
// a `.fused` and `FetchAppFileBoot` opens it. Mirrored in deeplink.py
// (`FETCH_APPGIT_PARAM`).
export const FETCH_APPGIT_PARAM = "_fetch_appgit";

/** The value of a boot hand-off param in `search`, or null. Empty is null
 *  too: a bare `?_edit_appfile=` names nothing to open. */
export function bootParamFromSearch(search: string, name: string): string | null {
  const v = new URLSearchParams(search).get(name);
  return v ? v : null;
}

/** `url` (path + search) with the hand-off param removed and the rest of the
 *  query kept in order; no trailing `?` when nothing is left. */
export function withoutBootParam(url: string, name: string): string {
  const q = url.indexOf("?");
  if (q < 0) return url;
  const params = new URLSearchParams(url.slice(q + 1));
  params.delete(name);
  const rest = params.toString();
  return url.slice(0, q) + (rest ? "?" + rest : "");
}

/** The .fused path a boot URL's search carries, or null. */
export function editAppFileFromSearch(search: string): string | null {
  return bootParamFromSearch(search, EDIT_APPFILE_PARAM);
}

export function withoutEditAppFile(url: string): string {
  return withoutBootParam(url, EDIT_APPFILE_PARAM);
}

/** The http(s) .fused link a boot URL's search carries, or null. */
export function fetchAppFileFromSearch(search: string): string | null {
  return bootParamFromSearch(search, FETCH_APPFILE_PARAM);
}

export function withoutFetchAppFile(url: string): string {
  return withoutBootParam(url, FETCH_APPFILE_PARAM);
}

/** The GitHub repo link a boot URL's search carries, or null. */
export function fetchAppGitFromSearch(search: string): string | null {
  return bootParamFromSearch(search, FETCH_APPGIT_PARAM);
}

export function withoutFetchAppGit(url: string): string {
  return withoutBootParam(url, FETCH_APPGIT_PARAM);
}
