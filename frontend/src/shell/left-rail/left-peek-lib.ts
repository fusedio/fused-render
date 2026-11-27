// SIDEBAR PREVIEWS, the parts that need no DOM (owner, 2026-11-27; the story
// is left-peek-store's header): what each collapsed-sidebar icon's card shows,
// the frame addresses, and what a press means to a left card.
import { EMBED_PARAM, EMBED_PREFIX, PREVIEW_PARAM, VIEW_PREFIX, withPreviewFlag } from "@platform/lib/router";
import { NO_FOCUS_PARAM } from "@platform/lib/frame-focus";
import type { PressTarget, PressWhere } from "@shell/claude-rail/rail-pin-lib";

/** WHAT A LEFT CARD SHOWS:
 *    - `frame`: a page, chrome-free, in the card (Home's explorer, AI Models,
 *      the Community, a canvas's canvas.toml view, a favourite);
 *    - `search`: Home's file search alone, in a frame whose result opens on
 *      the page, not in the card (LeftPeek);
 *    - `app`: a project — its `preview.png` still while a preview, its live
 *      entry page once pinned, or when it has no still. */
export type LeftPeekShow =
  | { kind: "frame"; src: string }
  | { kind: "search"; src: string }
  | { kind: "app"; still: string; picture: string; live: string }
  // The "+ New app" card: the New-app composer itself (the modal's content),
  // drawn in the card — no page, no frame.
  | { kind: "compose" };

/** The Home page narrowed to its file search (shell/Home.tsx `SEARCH_ONLY`). */
export const SEARCH_ONLY_PARAM = "searchonly";
export const SEARCH_CARD_SRC = `/home?${EMBED_PARAM}=1&${SEARCH_ONLY_PARAM}=1`;

/** `url` with one query param set: only its search part changes — the path
 *  and a `#fragment` are kept, and a second literal `?` stays inside the
 *  query (its first `?` starts the query, its first `#` ends it). */
export function withSearchParam(url: string, name: string, value: string): string {
  const hashAt = url.indexOf("#");
  const base = hashAt < 0 ? url : url.slice(0, hashAt);
  const hash = hashAt < 0 ? "" : url.slice(hashAt);
  const qAt = base.indexOf("?");
  const path = qAt < 0 ? base : base.slice(0, qAt);
  const q = new URLSearchParams(qAt < 0 ? "" : base.slice(qAt + 1));
  if (q.get(name) === value) return url;
  q.set(name, value);
  return path + "?" + q.toString() + hash;
}

/** A shell route's address with the query spelling of embed mode
 *  (`?embed=1`, router IS_QUERY_EMBED), kept once. */
export function withQueryEmbed(url: string): string {
  return withSearchParam(url, EMBED_PARAM, "1");
}

/** An explorer page with its companion pane (Claude, Git) shut: in a card
 *  the pane would take most of the width and leave the listing a sliver. */
export function sideOff(url: string): string {
  return withSearchParam(url, "_side", "off");
}

/** A FAVOURITE'S CARD: an explorer page's own embed (`/explorer/view/…` →
 *  `/explorer/embed/…`, its query kept), any other shell route with
 *  `?embed=1`; anything that is not a path on this server (an `http…`
 *  address, `//host`, an `s3://` one) has no card. */
export function bookmarkFrameSrc(url: string): string | null {
  if (!url || !url.startsWith("/") || url.startsWith("//") || url.startsWith("/api/")) return null;
  if (url.startsWith(VIEW_PREFIX)) return sideOff(EMBED_PREFIX + url.slice(VIEW_PREFIX.length));
  if (url.startsWith(EMBED_PREFIX)) return sideOff(url);
  return withQueryEmbed(url);
}

/** A PROJECT'S CARD (owner, 2026-11-27: "standup recap / pin remake were not
 *  working fine"): its folder's `preview.png` (`still`, through `rawUrl`,
 *  which the caller hands in) while a preview; a preview with no still shows
 *  the entry page as a PICTURE (`picture`: the thumbnail stamp, `_preview=1`
 *  — no open recorded, D301, and the runtime refuses the app's daemon,
 *  connect and udf calls, so an app paints its poster face); once the card
 *  is pressed or held, the REAL page (`live`: no stamp) — the app works.
 *  Every one of them in the explorer's embed with its Claude side pane shut
 *  (`_side=off`: "only explorer or app view" in a card that small). No entry
 *  page: the folder itself. */
export function appShow(o: { path: string; entry: string | null; rawUrl: (fsPath: string) => string; embedUrl: (fsPath: string) => string }): LeftPeekShow {
  const folder = o.path.replace(/\/+$/, "");
  const live = sideOff(o.embedUrl(o.entry ?? o.path));
  return { kind: "app", still: o.rawUrl(folder + "/preview.png"), picture: withPreviewFlag(live), live };
}

/** A CANVAS'S CARD: the canvas.toml view (templates/canvas, SPEC §28) of its
 *  clone, in an explorer embed, fitted to the whole canvas (`fit=1`) rather
 *  than at its saved viewport. */
export function canvasShow(o: { path: string; embedUrl: (fsPath: string, search?: string) => string }): LeftPeekShow {
  return { kind: "frame", src: o.embedUrl(o.path.replace(/\/+$/, "") + "/canvas.toml", "?_mode=canvas&fit=1") };
}

/** Which address a card frames now: a project's REAL page once pinned
 *  (pressed inside, or held); while a preview, its picture when the still
 *  failed, else nothing (the still is drawn). The press changing the address
 *  reloads the frame once. */
export function frameSrcOf(show: LeftPeekShow, o: { pinned: boolean; stillFailed: boolean }): string | null {
  if (show.kind === "compose") return null;
  if (show.kind !== "app") return show.src;
  if (o.pinned) return show.live;
  return o.stillFailed ? show.picture : null;
}

/** THE SEARCH CARD'S RESULT OPENS ON THE PAGE: where the search frame went
 *  (its path and query), as the top page's address — the embed prefix back
 *  to the view prefix, the frame's own stamps dropped. Null while the frame
 *  is still on its search (nothing to open). */
export function searchHopTarget(pathAndSearch: string): string | null {
  const qAt = pathAndSearch.indexOf("?");
  const path = qAt < 0 ? pathAndSearch : pathAndSearch.slice(0, qAt);
  const query = qAt < 0 ? "" : pathAndSearch.slice(qAt + 1);
  if (path === "/home") return null;
  const q = new URLSearchParams(query);
  for (const k of [EMBED_PARAM, SEARCH_ONLY_PARAM, NO_FOCUS_PARAM, PREVIEW_PARAM]) q.delete(k);
  const top = path.startsWith(EMBED_PREFIX) ? VIEW_PREFIX + path.slice(EMBED_PREFIX.length) : path;
  const qs = q.toString();
  return top + (qs ? "?" + qs : "");
}

/** The attribute every left door carries: its item's key. */
export const LEFT_DOOR_ATTR = "data-left-door";
/** …and while its card is up: `open` (a preview or temp card), `pinned`. */
export const LEFT_OPEN_ATTR = "data-left-open";

function cssString(v: string): string {
  return v.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/** One left door, for the outside-press classifier. */
export function leftDoorSelector(key: string): string {
  return `[${LEFT_DOOR_ATTR}="${cssString(key)}"]`;
}

/** The left sidebar — collapsed to its rail or expanded, and the width seam
 *  riding its edge — and every left card and bridge. */
export const LEFT_RAIL_SELECTOR = "#sidebar, .sidebar-resize-handle";
export const LEFT_CARD_ANY = ".claude-rail-peek.is-left";
export const LEFT_BRIDGE = ".claude-rail-peek-bridge.is-left";

/** WHERE A PRESS LANDED, for one LEFT card (rail-pin-lib `pressVerdict`'s
 *  words, the left sidebar's view): its own card, its bridge or anything it
 *  opened is `inside`; its own icon or row is `own`; the rest of the sidebar
 *  (collapsed or expanded) and the other left cards are the `view` (they
 *  close nothing);
 *  everything else — the page, a frame, the CLAUDE RAIL and its cards — is
 *  `outside`. */
export function leftPressVerdict(
  el: PressTarget | null,
  o: { pressedInside: boolean; cardSelectors: readonly string[]; ownSelectors: readonly string[] },
): PressWhere {
  if (o.pressedInside) return "inside";
  if (!el) return "outside";
  for (const s of o.cardSelectors) if (el.closest(s)) return "inside";
  if (el.closest(LEFT_BRIDGE)) return "inside";
  for (const s of o.ownSelectors) if (el.closest(s)) return "own";
  if (el.closest(LEFT_RAIL_SELECTOR) || el.closest(LEFT_CARD_ANY)) return "view";
  return "outside";
}

/** IS THE FRAMED PAGE SHOWING SOMETHING YET (LeftPeek's spinner)? The frame's
 *  `load` only says the shell's document arrived: an explorer embed then
 *  resolves its view and loads the file's own frame, and until that frame is
 *  the shown one the card is a blank ground. Ready when the shell has drawn
 *  (its root has content), no view is still resolving, and — where the page
 *  is a file's preview — its frame is the shown one. `doc` is the framed
 *  page's document (a stub in a test). */
export function framePainted(doc: { querySelector(selector: string): unknown } | null | undefined): boolean {
  if (!doc) return false;
  if (!doc.querySelector("#root > *, body > :not(script):not(#root)")) return false;
  if (doc.querySelector(".preview-resolving")) return false;
  if (doc.querySelector(".preview-body iframe") && !doc.querySelector(".preview-body iframe.is-shown, .preview-body .is-shown iframe")) return false;
  return true;
}
