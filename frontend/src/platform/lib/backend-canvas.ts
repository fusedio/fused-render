// The ONE hosted canvas fused-render's backend lives on — the page-side
// mirror of fused_render/backend_canvas.py (owner 2026-10-27: "only have 1
// canvas for all the backend required in fused-render"). Change the host or
// the share token THERE and here, nowhere else. The server reads the canvas
// for everything that needs a request (marketplace.py); the page only builds
// links a person opens (the listing page, the GitHub install page).
//
// Not here: the canvases the user's own Fused account creates for public
// sharing (share-app.ts) — those are the user's, not the backend's.

/** Unstable for now; prod when the canvas moves. */
export const BACKEND_CANVAS_HOST = "https://unstable.udf.ai";
/** The canvas's public share token. */
export const BACKEND_CANVAS_TOKEN = "fc_5OHf9uj6AGOKqLRH25kkFU";
/** `<host>/<token>` — what every UDF URL starts with. */
export const BACKEND_CANVAS_BASE = `${BACKEND_CANVAS_HOST}/${BACKEND_CANVAS_TOKEN}`;

/** The public listing page of a marketplace app (§18.3 B6). */
export function listingPageUrl(slug: string): string {
  return `${BACKEND_CANVAS_BASE}/fused_app_listing?slug=${encodeURIComponent(slug)}`;
}

/** The GitHub install page for `<owner>/<repo>` (share_app.py's install link). */
export function githubLinkUrl(repo: string): string {
  return `${BACKEND_CANVAS_BASE}/fused_app_github.html?repo=${encodeURI(repo)}`;
}
