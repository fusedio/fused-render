// SIDEBAR PREVIEWS, the parts that need no DOM (owner, 2026-11-27; the story
// is left-peek-store's header): what each collapsed-sidebar icon's card shows,
// the frame addresses, and what a press means to a left card.
import { EMBED_PARAM, EMBED_PREFIX, PREVIEW_PARAM, VIEW_PREFIX, withPreviewFlag } from "@platform/lib/router";
import { NO_FOCUS_PARAM } from "@platform/lib/frame-focus";
import type { PressTarget, PressWhere } from "@shell/claude-rail/rail-pin-lib";

/** WHAT A LEFT CARD SHOWS:
 *    - `frame`: a page, chrome-free, in the card (Home's explorer, AI Models,
 *      the Community, a canvas's canvas.toml view, a favourite); with
 *      `searchHome`, a file search box in the card's head over that folder —
 *      typing swaps the page for the results (the Home card, LeftPeek);
 *    - `app`: a project — its `preview.png` still while a preview, its live
 *      entry page once pinned, or when it has no still. */
export type LeftPeekShow =
  | { kind: "frame"; src: string; searchHome?: string }
  | { kind: "app"; still: string; picture: string; live: string }
  // The "+ New app" card: the New-app composer itself (the modal's content),
  // drawn in the card — no page, no frame.
  | { kind: "compose" };

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

/** THE HOME CARD'S TRAIL (owner, 2026-11-27: "we open everything in card
 *  unless user clicks open in top right, when it does we open whatever view
 *  we are in"): what the card has shown, oldest first — a framed page
 *  (`page`: the address the frame is on, as the frame sees it) or the search
 *  results (`results`). The last step is what shows; Back drops it. The
 *  first step (the ~ listing) never goes. */
export type HomeStep = { kind: "results" } | { kind: "page"; url: string };

/** A query became live: its results show on top (once). */
export function trailResults(trail: readonly HomeStep[]): HomeStep[] {
  return trail[trail.length - 1]?.kind === "results" ? [...trail] : [...trail, { kind: "results" }];
}

/** The query emptied (Esc, Clear, the box wiped): its results go — on top,
 *  or under a page they led to (Back never lands on empty results). */
export function trailNoResults(trail: readonly HomeStep[]): HomeStep[] {
  return trail.filter((step, i) => i === 0 || step.kind !== "results");
}

/** A page opened in the card (a result picked): it shows on top. */
export function trailOpen(trail: readonly HomeStep[], url: string): HomeStep[] {
  return [...trail, { kind: "page", url }];
}

function pathOf(url: string): string {
  const cut = url.search(/[?#]/);
  return cut < 0 ? url : url.slice(0, cut);
}

/** THE FRAME WENT SOMEWHERE ON ITS OWN (a folder opened in the listing): a
 *  new path is a new step; the same path with another query (a view
 *  param, a selection) only updates the step it is. Nothing while the
 *  results are on top — the frame under them is not what shows. */
export function trailFrameAt(trail: readonly HomeStep[], url: string): HomeStep[] {
  const top = trail[trail.length - 1];
  if (!top || top.kind !== "page") return [...trail];
  if (top.url === url) return [...trail];
  if (pathOf(top.url) === pathOf(url)) return [...trail.slice(0, -1), { kind: "page", url }];
  return [...trail, { kind: "page", url }];
}

/** Back: the step on top goes (never the first). */
export function trailBack(trail: readonly HomeStep[]): HomeStep[] {
  return trail.length > 1 ? trail.slice(0, -1) : [...trail];
}

/** The page the frame should be on: the last page step. */
export function trailPage(trail: readonly HomeStep[]): string | null {
  for (let i = trail.length - 1; i >= 0; i--) {
    const s = trail[i];
    if (s.kind === "page") return s.url;
  }
  return null;
}

/** A FRAMED PAGE AS THE TOP PAGE'S ADDRESS (the card's Open): the embed
 *  prefix back to the view prefix, the card's own stamps dropped (`embed`,
 *  `_nofocus`, `_preview`, and the shut side pane `_side=off`, so the page
 *  keeps its own pane). */
export function cardPageUrl(url: string): string {
  const hashAt = url.indexOf("#");
  const base = hashAt < 0 ? url : url.slice(0, hashAt);
  const hash = hashAt < 0 ? "" : url.slice(hashAt);
  const qAt = base.indexOf("?");
  const path = qAt < 0 ? base : base.slice(0, qAt);
  const q = new URLSearchParams(qAt < 0 ? "" : base.slice(qAt + 1));
  for (const k of [EMBED_PARAM, NO_FOCUS_PARAM, PREVIEW_PARAM]) q.delete(k);
  if (q.get("_side") === "off") q.delete("_side");
  const top = path.startsWith(EMBED_PREFIX) ? VIEW_PREFIX + path.slice(EMBED_PREFIX.length) : path;
  const qs = q.toString();
  return top + (qs ? "?" + qs : "") + hash;
}

/** WHAT A CARD'S OPEN LOADS, for a card that keeps a trail (the Home card;
 *  in a workspace host every card with a page — owner, 2026-11-28: "it
 *  navigates and opens things inside unless I click Open"): the view the
 *  card is on. `base` is the address the card came up on, `top` the step
 *  that shows, `query` the search box's words.
 *    - the search results: Home with the query (`homeSearchUrl`);
 *    - the Home card (`searchHome`): the framed page, always;
 *    - any other card still on the page it came up on: `null` — the entry's
 *      own Open (a favourite's arms it, a project's is its app page);
 *    - any other card that went somewhere: that page. */
export function cardOpenTarget(o: { base: string; top: HomeStep; searchHome: boolean; query: string }): string | null {
  if (o.top.kind === "results") return homeSearchUrl(o.query);
  const at = cardPageUrl(o.top.url);
  if (o.searchHome) return at;
  return at === cardPageUrl(o.base) ? null : at;
}

/** The Home page with a search typed (the card's Open while its results
 *  show) — an INSTANT one (`qmode=instant`, SearchWidget): a bare `?q=` is a
 *  committed AI search, which Home would run again (a model call). */
export function homeSearchUrl(query: string): string {
  return query.trim() ? "/home?q=" + encodeURIComponent(query) + "&qmode=instant" : "/home";
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

// ---- in a workspace host (SPEC §56 WS-28…WS-31) ------------------------------
//
// Owner, 2026-11-28: "We still show the hover preview popup on both sides …
// but a click changes the left / right content. Only for FAVOURITES … we keep
// hover preview, click preview and pin." Outside a host every answer below is
// today's, so the flag off changes nothing.

/** Which kind of entry, for the rules below: a favourite (a bookmark), the
 *  Search row, or any other entry (AI Models, Community, a project, a canvas,
 *  the "+" rows). */
export type LeftEntryKind = "bookmark" | "search" | "other";

/** WHAT A PLAIN CLICK ON A LEFT ENTRY DOES:
 *    - `page`: the entry's own click (its page, in the tab);
 *    - `card`: its card comes up, held till one press outside (temp) — a
 *      favourite's in a host, or Search where there is no launcher;
 *    - `launcher`: the Search row's Mac launcher.
 *  `hasCard`: the entry has a card that can stand now; `active`: the active
 *  tab shows the entry's page (a favourite then just opens, harmlessly). */
export function leftEntryPress(o: {
  workspace: boolean;
  peekOn: boolean;
  kind: LeftEntryKind;
  hasCard: boolean;
  active: boolean;
  launcher: boolean;
}): "page" | "card" | "launcher" {
  const cardOk = o.workspace && o.peekOn && o.hasCard;
  if (o.kind === "search") return o.launcher || !cardOk ? "launcher" : "card";
  if (o.kind === "bookmark" && cardOk && !o.active) return "card";
  return "page";
}

/** THE SEARCH ROW STANDS IN A HOST ONLY, in Home's place, and where it can
 *  do something: the launcher, or the Home card it carries (the file search).
 *  Outside a host the Home row has that card and there is no Search row. */
export function leftSearchRowShown(o: { workspace: boolean; launcher: boolean; peekOn: boolean }): boolean {
  return o.workspace && (o.launcher || o.peekOn);
}

/** A LEFT CARD CARRIES A PIN (its button, a head drag): always outside a
 *  host; in one, only a favourite's. */
export function leftCardPinnable(o: { workspace: boolean; kind: LeftEntryKind }): boolean {
  return !o.workspace || o.kind === "bookmark";
}

/** A HOVER OR ARROWRIGHT OPENS THE ENTRY'S CARD, unless its page is the one
 *  on screen (owner, 2026-11-27: "if the page is active it shouldn't popup")
 *  — in a host, the page the active tab shows. */
export function leftCardOnHover(o: { active: boolean }): boolean {
  return !o.active;
}

/** Where a plain press on an entry landed: on its own link (the rail icon
 *  itself, a row's link) — the press of a click — or on something else in
 *  it (a row's glyph, a button, a drag's start). */
export function pressOnLink(target: PressTarget | null, entry: unknown): boolean {
  if (!target) return false;
  const hit = target.closest("a[href], button") as (PressTarget & { matches?: (s: string) => boolean }) | null;
  if (!hit) return false;
  return hit === entry || (typeof hit.matches === "function" && hit.matches("a[href]"));
}

/** The bookmark a Shift+click in the sidebar landed on: a row's id (the
 *  expanded Favourites), else its card's key (`pin:<id>`), else the first
 *  pinned bookmark at the link's address (a collapsed rail icon). Null for
 *  anything else, or an address that is not this server's. */
export function shiftClickBookmark(
  link: PressTarget & { getAttribute(name: string): string | null },
  pins: readonly { id: string; url: string }[],
): { id: string; url: string } | null {
  const href = link.getAttribute("href");
  if (!href || !href.startsWith("/") || href.startsWith("//")) return null;
  const row = link.closest(".bookmark-row[data-id]") as { getAttribute(name: string): string | null } | null;
  const door = link.closest(`[${LEFT_DOOR_ATTR}^="pin:"]`) as { getAttribute(name: string): string | null } | null;
  const id = row?.getAttribute("data-id") ?? door?.getAttribute(LEFT_DOOR_ATTR)?.slice(4) ?? null;
  if (id) return { id, url: href };
  if (!link.closest(".sidebar-rail-btn")) return null;
  const pin = pins.find((b) => b.url === href);
  return pin ? { id: pin.id, url: href } : null;
}
