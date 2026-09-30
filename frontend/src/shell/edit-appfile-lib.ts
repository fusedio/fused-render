// The `?_edit_appfile=<path>` hand-off (SPEC §26 DL-7, D889): the server's
// GET /clone turns a `fused-render://open?file=` deep link — Render App's Edit
// button — into a redirect INTO the shell with the .fused's absolute path in
// this param. `EditAppFileBoot` reads it exactly once at boot and strips it,
// so a reload or Back never re-runs the hand-off. Pure string work here so it
// is testable without a DOM; the name is mirrored in deeplink.py
// (`EDIT_APPFILE_PARAM`).
export const EDIT_APPFILE_PARAM = "_edit_appfile";

/** The .fused path a boot URL's search carries, or null. Empty is null too:
 *  a bare `?_edit_appfile=` names nothing to open. */
export function editAppFileFromSearch(search: string): string | null {
  const v = new URLSearchParams(search).get(EDIT_APPFILE_PARAM);
  return v ? v : null;
}

/** `url` (path + search) with the hand-off param removed and the rest of the
 *  query kept in order; no trailing `?` when nothing is left. */
export function withoutEditAppFile(url: string): string {
  const q = url.indexOf("?");
  if (q < 0) return url;
  const params = new URLSearchParams(url.slice(q + 1));
  params.delete(EDIT_APPFILE_PARAM);
  const rest = params.toString();
  return url.slice(0, q) + (rest ? "?" + rest : "");
}
