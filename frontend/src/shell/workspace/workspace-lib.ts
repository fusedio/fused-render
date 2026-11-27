// The workspace, the pure half (D1514, SPEC §56; WorkspaceHost.tsx is the
// React half): the host URL's `_tabs` / `_right` codec, which page a url names,
// the open rules, tab labels and the neighbour after a close.
//
// DOM-FREE AT IMPORT, so bun can test it: nothing here reads `window`,
// `location` or the router (router.ts reads `window` at module init).
//
// Every query edit is TEXTUAL, segment by segment, never a URLSearchParams
// round trip: that re-encodes every other param (`_layout=(…)` is not even
// URLSearchParams grammar, layout-codec D51), so a host URL would come back
// byte-different from the one it was built from and the mirror's "only when
// changed" guard would write on every sync.

import { rootedFsPath } from "@platform/lib/path-url";
import { appPathFromPath } from "@shell/current-apps-lib";
import type { RailPlace } from "@shell/claude-rail/claude-rail-lib";
import type { WorkbenchEnv } from "@apps/canvases/api";

export const HOME_URL = "/home";
export const TABS_PARAM = "_tabs";
export const RIGHT_PARAM = "_right";

// The active tab's slot in `_tabs`: a literal `.` (its url IS the host's own
// location). Every real item is `encodeURIComponent(path+query)` and so starts
// `%2F`, so neither marker can collide with one.
const ACTIVE_SLOT = ".";
// …or, when the active tab is not a page of this SPA (a `/render?…` runtime
// page, which the server answers with the page itself — a host reloaded there
// would come back as a bare app), the host sits on Home and names it here.
const EXPLICIT_ACTIVE = "!";

interface UrlParts {
  path: string;
  // Without its leading `?`; "" when there is none.
  query: string;
  // With its leading `#`; "" when there is none.
  hash: string;
}

function splitUrl(url: string): UrlParts {
  const h = url.indexOf("#");
  const hash = h === -1 ? "" : url.slice(h);
  const rest = h === -1 ? url : url.slice(0, h);
  const q = rest.indexOf("?");
  return {
    path: q === -1 ? rest : rest.slice(0, q),
    query: q === -1 ? "" : rest.slice(q + 1),
    hash,
  };
}

function joinUrl({ path, query, hash }: UrlParts): string {
  return path + (query ? "?" + query : "") + hash;
}

function querySegments(query: string): string[] {
  return query ? query.split("&").filter((s) => s.length > 0) : [];
}

function paramOf(query: string, name: string): string | null {
  const seg = querySegments(query).find((s) => s.startsWith(name + "="));
  return seg === undefined ? null : seg.slice(name.length + 1);
}

function withoutParams(query: string, names: string[]): string {
  return querySegments(query)
    .filter((s) => !names.some((n) => s === n || s.startsWith(n + "=")))
    .join("&");
}

/** A url without its hash: what a tab records (a frame's live location is
 *  read as pathname+search) and what the host URL lists. */
export function stripHash(url: string): string {
  const h = url.indexOf("#");
  return h === -1 ? url : url.slice(0, h);
}

// THE SHELL'S OWN ROUTES — documents whose router can be asked to navigate in
// place (router.ts NAVIGATE_URL_EVENT) and locations the host may sit on. A
// closed list (App.tsx's dispatch; keep in step when a route is added) because
// the alternative, "anything not /render", would call every page the server
// answers on its own an SPA page. NOT `/onboarding`: the setup wizard is the
// whole window, never a tab.
const SHELL_ROUTES = new Set([
  "/",
  "/home",
  "/apps",
  "/tasks",
  "/preferences",
  "/templates",
  "/monitor",
  "/marketplace",
  "/canvases",
  "/bots",
  "/ai-models",
  "/explorer",
  "/claude-config",
  "/claude-md",
  "/skills",
  "/install",
]);
const SHELL_ROUTE_PREFIXES = [
  "/apps/",
  "/canvases/",
  "/bots/",
  "/ai-models/",
  "/marketplace/",
  "/explorer/view",
  "/explorer/embed",
];

/** Is `url` a page of this SPA (see SHELL_ROUTES)? */
export function isSpaUrl(url: string): boolean {
  const { path } = splitUrl(url);
  return path === "" || SHELL_ROUTES.has(path) || SHELL_ROUTE_PREFIXES.some((p) => path.startsWith(p));
}

// May the HOST document sit on `url`? Only an SPA page that is not an embed
// spelling: IS_EMBED is read once at load, so a host reloaded on
// `/explorer/embed/…` or `?embed=1` would boot chrome-free and never be a host
// again. Such an active tab is named explicitly (`!`) like a non-SPA one.
function hostCanSit(url: string): boolean {
  if (!isSpaUrl(url)) return false;
  const { path, query } = splitUrl(url);
  if (path.startsWith("/explorer/embed")) return false;
  // DECODED, as the page itself reads it (router.ts IS_QUERY_EMBED goes
  // through URLSearchParams): `embed=%31` boots an embed too.
  return !querySegments(query).some((seg) => {
    try {
      return decodeURIComponent(seg.replace(/\+/g, " ")) === "embed=1";
    } catch {
      return false;
    }
  });
}

/** A path+query this origin serves, as a browser would resolve it. Starting
 *  with one `/` is not enough: the URL parser reads `\` as `/` and drops tabs
 *  and newlines before resolving, so `/\evil.example/x` and `/<tab>/evil…` name
 *  another origin. Nothing a tab's own URL holds needs those characters raw. */
export function isPlainPath(url: string): boolean {
  if (!url.startsWith("/") || url.startsWith("//")) return false;
  return !/[\\\u0000-\u001f\u007f]/.test(url);
}

/** Is `url` Home (`/` is the front door, which is Home)? */
export function isHomeUrl(url: string): boolean {
  const { path } = splitUrl(url);
  return path === HOME_URL || path === "/" || path === "";
}

function decodePath(path: string): string {
  try {
    return decodeURI(path);
  } catch {
    return path;
  }
}

function isLayoutSentinel(path: string): boolean {
  return /^\/explorer\/(view|embed)\/_(panel|tab)$/.test(path);
}

/**
 * Two urls name the same PAGE — what the open rules ask before opening a tab.
 * The decoded pathname, query and hash ignored (a tab that has since changed
 * its sort is still that page), `/` the same as `/home`. Where the QUERY is the
 * page it counts too: a non-SPA page (`/render` is one path for every app) and
 * the explorer's `_panel`/`_tab` sentinels, whose whole layout is the query.
 */
export function samePage(a: string, b: string): boolean {
  const pa = splitUrl(a);
  const pb = splitUrl(b);
  const norm = (p: string) => (p === "/" || p === "" ? HOME_URL : decodePath(p));
  if (norm(pa.path) !== norm(pb.path)) return false;
  if (!isSpaUrl(a) || isLayoutSentinel(pa.path)) return pa.query === pb.query;
  return true;
}

/** A requested url, reduced to what a tab can hold: a same-origin
 *  path+query+hash, or a foreign href to open outside the app. */
export type NormalizedUrl = { kind: "same"; url: string } | { kind: "foreign"; href: string };

export function normalizeOpenUrl(raw: string, base: string): NormalizedUrl | null {
  try {
    const u = new URL(raw, base);
    const origin = new URL(base).origin;
    if (u.origin !== origin) {
      // Only web links leave the app; `javascript:` and friends go nowhere.
      return u.protocol === "http:" || u.protocol === "https:" ? { kind: "foreign", href: u.href } : null;
    }
    return { kind: "same", url: u.pathname + u.search + u.hash };
  } catch {
    return null;
  }
}

/** What an open does (SPEC §56 WS-5). */
export type OpenPlan =
  | { kind: "activate"; index: number; navigate: boolean }
  | { kind: "append" }
  | { kind: "navigateActive" };

/**
 * The open rules, for a strip of tab `urls` (Home at 0) with `active` shown:
 *   - a tab already on that page → activate it (never two tabs on one page),
 *     and navigate it too when the request carries its own query or hash and
 *     differs from where the tab is (a bookmark's saved params must apply);
 *     a bare url only switches, so the tab keeps its state;
 *   - else Shift (`newTab`) → a new tab at the end;
 *   - else from Home → a new tab at the end (Home never leaves Home);
 *   - else → the active tab goes there.
 */
export function planOpen(urls: readonly string[], active: number, url: string, newTab = false): OpenPlan {
  const hit = urls.findIndex((u) => samePage(u, url));
  if (hit !== -1) {
    const { query, hash } = splitUrl(url);
    const navigate = (query !== "" || hash !== "") && url !== urls[hit];
    return { kind: "activate", index: hit, navigate };
  }
  if (newTab || active === 0) return { kind: "append" };
  return { kind: "navigateActive" };
}

/**
 * The tab to activate after closing index `closed` of `count`, as an index
 * into the strip AFTER the close: the right neighbour (it slides into the
 * slot), else the left. Home (0) is never closed, so the answer is ≥ 0.
 */
export function neighborAfterClose(closed: number, count: number): number {
  const left = count - 1;
  if (left <= 0) return 0;
  return Math.min(closed, left - 1);
}

/**
 * The `_tabs` value for `urls` with `active` shown: every other item
 * `encodeURIComponent`-ed, comma-joined, the active slot `.` (or `!<encoded>`
 * for a non-SPA page). `null` for Home alone: one tab is just a window.
 */
export function encodeTabsParam(urls: readonly string[], active: number): string | null {
  const activeIsSpa = hostCanSit(urls[active] ?? HOME_URL);
  if (urls.length <= 1 && activeIsSpa) return null;
  return urls
    .map((u, i) =>
      i !== active ? encodeURIComponent(u) : activeIsSpa ? ACTIVE_SLOT : EXPLICIT_ACTIVE + encodeURIComponent(u),
    )
    .join(",");
}

/**
 * The whole host URL: the active tab's own url (Home, for a non-SPA one) with
 * `_tabs` and then `_right` appended last. `right` is opaque here — stage 1
 * only carries it through.
 */
export function buildHostUrl(urls: readonly string[], active: number, right: string | null = null): string {
  const activeUrl = stripHash(urls[active] ?? urls[0] ?? HOME_URL);
  const tabs = encodeTabsParam(urls, active);
  const parts = splitUrl(hostCanSit(activeUrl) ? activeUrl : HOME_URL);
  const segs = querySegments(withoutParams(parts.query, [TABS_PARAM, RIGHT_PARAM]));
  if (tabs !== null) segs.push(TABS_PARAM + "=" + tabs);
  if (right !== null) segs.push(RIGHT_PARAM + "=" + right);
  return joinUrl({ path: parts.path || HOME_URL, query: segs.join("&"), hash: "" });
}

export interface ParsedHost {
  tabs: string[];
  active: number;
  right: string | null;
}

/**
 * Parse a host `pathname+search` back into the strip. Always answers with Home
 * at 0: a list that does not start with Home gets it (moved to the front, or
 * prepended), so a window turned into a host while on a file is `[Home, file]`
 * with the file active. Malformed input degrades instead of throwing: an
 * undecodable or foreign item is dropped; a list with no active slot puts the
 * location itself in as the active tab; a second active slot is ignored (`.`)
 * or read as an ordinary tab (`!`).
 */
export function parseHostUrl(url: string): ParsedHost {
  const parts = splitUrl(stripHash(url));
  const rawTabs = paramOf(parts.query, TABS_PARAM);
  const right = paramOf(parts.query, RIGHT_PARAM);
  const own = joinUrl({ ...parts, query: withoutParams(parts.query, [TABS_PARAM, RIGHT_PARAM]) });
  let tabs: string[] = [];
  let active = -1;
  if (rawTabs === null) {
    tabs = [own];
    active = 0;
  } else {
    for (const item of rawTabs.split(",")) {
      if (item === ACTIVE_SLOT) {
        if (active !== -1) continue;
        active = tabs.length;
        tabs.push(own);
        continue;
      }
      const explicit = item.startsWith(EXPLICIT_ACTIVE);
      const body = explicit ? item.slice(EXPLICIT_ACTIVE.length) : item;
      if (!body) continue;
      let decoded: string;
      try {
        decoded = decodeURIComponent(body);
      } catch {
        continue;
      }
      // Same-origin paths only: an item becomes an iframe src, so a
      // hand-edited `https://…` or `javascript:` must not become a tab.
      if (!isPlainPath(decoded)) continue;
      if (explicit && active === -1) active = tabs.length;
      tabs.push(stripHash(decoded));
    }
    if (active === -1) {
      tabs.unshift(own);
      active = 0;
    }
  }
  // Home is tab 0, always.
  const home = tabs.findIndex(isHomeUrl);
  if (home === -1) {
    tabs.unshift(HOME_URL);
    active += 1;
  } else if (home > 0) {
    const [h] = tabs.splice(home, 1);
    tabs.unshift(h);
    if (active === home) active = 0;
    else if (active < home) active += 1;
  }
  // `/` is the front door; Home's own spelling is `/home`.
  const h = splitUrl(tabs[0]);
  if (h.path !== HOME_URL) tabs[0] = joinUrl({ ...h, path: HOME_URL });
  return { tabs, active, right };
}

/** The active tab's own URL for a host URL: the host URL minus `_tabs` and
 *  `_right` — what chrome comparing "the current page" must compare against. */
export function pageUrlOf(hostUrl: string): string {
  const parts = splitUrl(stripHash(hostUrl));
  const rawTabs = paramOf(parts.query, TABS_PARAM);
  if (rawTabs !== null) {
    const explicit = rawTabs.split(",").find((s) => s.startsWith(EXPLICIT_ACTIVE));
    if (explicit && !rawTabs.split(",").includes(ACTIVE_SLOT)) {
      try {
        const d = decodeURIComponent(explicit.slice(EXPLICIT_ACTIVE.length));
        if (d.startsWith("/") && !d.startsWith("//")) return d;
      } catch {
        /* fall through to the location itself */
      }
    }
  }
  return joinUrl({ ...parts, query: withoutParams(parts.query, [TABS_PARAM, RIGHT_PARAM]) });
}

/** What kind of page a tab shows — the strip's icon for it. */
export type TabKind =
  | "home"
  | "folder"
  | "file"
  | "app"
  | "canvas"
  | "models"
  | "community"
  | "tasks"
  | "bots"
  | "settings"
  | "page";

// The shell's own pages, by path: the name the page titles itself with (App's
// useDocumentTitle) and its kind. A tab that has not loaded yet (a reload
// mounts only the shown one) has no title to read, so the strip says this
// instead of a raw path segment. An entry ending in "/" is a prefix.
const ROUTES: readonly (readonly [string, string, TabKind])[] = [
  ["/marketplace", "Community", "community"],
  ["/marketplace/", "Community", "community"],
  ["/ai-models", "AI Models", "models"],
  ["/ai-models/", "AI Models", "models"],
  ["/preferences", "Preferences", "settings"],
  ["/templates", "Templates", "settings"],
  ["/claude-config", "Claude Code", "settings"],
  ["/skills", "Skills", "settings"],
  ["/monitor", "Monitor", "settings"],
  ["/apps", "Apps", "app"],
  ["/canvases", "Workbench Canvases", "canvas"],
  ["/explorer", "File Explorer", "folder"],
  ["/tasks", "Tasks", "tasks"],
  ["/bots", "Bots", "bots"],
  ["/bots/", "Bots", "bots"],
];

function routeOf(path: string): readonly [string, string, TabKind] | undefined {
  return ROUTES.find(([p]) => (p.endsWith("/") ? path.startsWith(p) : path === p));
}

/** The kind of page `url` names, from the URL alone. A folder and a file share
 *  the explorer's route, so the last segment decides: a dot in it reads as a
 *  file (a guess — the page knows; the strip only needs an icon). */
export function tabKind(url: string): TabKind {
  const { path } = splitUrl(url);
  if (path === "/" || path === HOME_URL) return "home";
  const route = routeOf(path);
  if (route) return route[2];
  if (path.startsWith("/apps/")) return "app";
  if (path.startsWith("/canvases/")) return "canvas";
  if (path.startsWith("/explorer/view/") || path.startsWith("/explorer/embed/")) {
    if (isLayoutSentinel(path)) return "page";
    const last = path.split("/").filter((s) => s.length > 0).pop() ?? "";
    return last.lastIndexOf(".") > 0 ? "file" : "folder";
  }
  return "page";
}

/**
 * A tab's strip label. The frame's own `document.title` wins (every route sets
 * one), minus the ` – <brand>` suffix the whole app appends — the strip is
 * inside the brand already. Fallbacks: "Home" for Home, a shell page's own
 * name, else the decoded last path segment (`?path=`'s leaf for a runtime
 * page).
 */
export function tabLabel(url: string, docTitle: string | null | undefined, brand: string): string {
  const { path, query } = splitUrl(url);
  if (path === "/" || path === HOME_URL) return "Home";
  const t = (docTitle || "").trim();
  if (t) {
    const suffix = " – " + brand;
    const bare = t.endsWith(suffix) ? t.slice(0, -suffix.length) : t;
    if (bare && bare !== brand) return bare;
  }
  const route = routeOf(path);
  if (route) return route[1];
  if (path === "/render") {
    const file = new URLSearchParams(query).get("path");
    const leaf = file?.split("/").filter((s) => s.length > 0).pop();
    if (leaf) return leaf;
  }
  const last = path.split("/").filter((s) => s.length > 0).pop();
  if (!last) return brand;
  try {
    return decodeURIComponent(last);
  } catch {
    return last;
  }
}

/** A tab frame's live `pathname+search`, or null (not loaded, cross-origin,
 *  gone). Duck-typed so it stays DOM-free at import. */
export function readFrameLoc(frame: { contentWindow: Window | null } | null | undefined): string | null {
  try {
    const loc = frame?.contentWindow?.location;
    if (!loc || loc.href === "about:blank") return null;
    return loc.pathname + loc.search;
  } catch {
    return null;
  }
}

// ---- the right panel (SPEC §56 WS-14…) -------------------------------------

/** What the right panel shows — the host URL's `_right`, decoded. */
export type RightView =
  | { kind: "tasks"; peek: string | null }
  | { kind: "bots"; url: string }
  | { kind: "chat"; file: string; query: string }
  | { kind: "git"; path: string }
  | { kind: "mode"; mode: string; file: string };

/** The chat params a chat door may carry into the panel: the ones that name
 *  WHICH conversation (and how to open it), never the page's own. */
export const CHAT_DOOR_PARAMS = ["session_id", "queued", "backend", "run", "msg"] as const;

function decodeArg(s: string): string | null {
  try {
    return decodeURIComponent(s);
  } catch {
    return null;
  }
}

/** Is `url` the Bots page or one of its pages? */
function isBotsUrl(url: string): boolean {
  const { path } = splitUrl(url);
  return path === "/bots" || path.startsWith("/bots/");
}

/**
 * The `_right` value, decoded. Every argument is `encodeURIComponent`-ed (which
 * always escapes `:`), so `:` only ever separates. Junk (an unknown kind, a
 * missing or undecodable argument) is null: the panel is closed.
 */
export function parseRight(raw: string | null | undefined): RightView | null {
  if (!raw) return null;
  const parts = raw.split(":");
  const args = parts.slice(1).map(decodeArg);
  if (args.some((a) => a === null)) return null;
  const a = args as string[];
  switch (parts[0]) {
    case "tasks":
      if (a.length > 1) return null;
      return { kind: "tasks", peek: a[0] ? a[0] : null };
    case "bots": {
      if (a.length > 1) return null;
      const url = a[0] || "/bots";
      return isBotsUrl(url) ? { kind: "bots", url } : null;
    }
    case "chat":
      if (a.length !== 2 || !a[0]) return null;
      return { kind: "chat", file: a[0], query: a[1].replace(/^\?/, "") };
    case "git":
      return a.length === 1 && a[0] ? { kind: "git", path: a[0] } : null;
    case "mode":
      return a.length === 2 && a[0] && a[1] ? { kind: "mode", mode: a[0], file: a[1] } : null;
    default:
      return null;
  }
}

/** …and back (`parseRight`'s inverse). */
export function encodeRight(view: RightView): string {
  const e = encodeURIComponent;
  switch (view.kind) {
    case "tasks":
      return view.peek ? "tasks:" + e(view.peek) : "tasks";
    case "bots":
      return view.url === "/bots" ? "bots" : "bots:" + e(view.url);
    case "chat":
      return "chat:" + e(view.file) + ":" + e(view.query.replace(/^\?/, ""));
    case "git":
      return "git:" + e(view.path);
    case "mode":
      return "mode:" + e(view.mode) + ":" + e(view.file);
  }
}

/** Do two views show the same thing? Two chats are the same conversation on
 *  the same file — their other params (`split`, `run`, …) are the chat's own
 *  state, not another chat. Opening what already shows does nothing. */
export function sameRight(a: RightView | null, b: RightView | null): boolean {
  if (!a || !b) return a === b;
  if (a.kind === "chat" && b.kind === "chat") {
    const pa = new URLSearchParams(a.query);
    const pb = new URLSearchParams(b.query);
    return a.file === b.file && (pa.get("session_id") || "") === (pb.get("session_id") || "") && (pa.get("queued") || "") === (pb.get("queued") || "");
  }
  return encodeRight(a) === encodeRight(b);
}

/** The query of a chat view, reduced to the door params present and
 *  non-empty, in `CHAT_DOOR_PARAMS` order. */
export function chatDoorQuery(query: string): string {
  const params = new URLSearchParams(query.replace(/^\?/, ""));
  const out = new URLSearchParams();
  for (const k of CHAT_DOOR_PARAMS) {
    const v = params.get(k);
    if (v) out.set(k, v);
  }
  return out.toString();
}

/** A chat view's session, or null. */
export function rightSession(view: RightView | null): string | null {
  if (!view || view.kind !== "chat") return null;
  return new URLSearchParams(view.query).get("session_id") || null;
}

/** Where a URL belongs in a workspace (WS-15): the right panel, the right
 *  panel once the host has found the canvas task by its session, or the
 *  left (the tab rules). */
export type NavPlace =
  | { side: "left" }
  | { side: "right"; view: RightView }
  | { side: "session"; sessionId: string };

const EXPLORER_VIEW = "/explorer/view";

/** The fs path an `/explorer/view/…` path names (router's own codec, DOM-free). */
function fsPathOfViewPath(path: string): string | null {
  if (path !== EXPLORER_VIEW && !path.startsWith(EXPLORER_VIEW + "/")) return null;
  const segs = path.slice(EXPLORER_VIEW.length).split("/").filter((s) => s.length > 0);
  const out: string[] = [];
  for (const s of segs) {
    const d = decodeArg(s);
    if (d === null) return null;
    out.push(d);
  }
  return out.length ? rootedFsPath(out.join("/")) : null;
}

/**
 * WHERE A TAB'S RAIL WOULD STAND, read off its URL alone (SPEC §56 WS-27): the
 * stand-in for a tab whose page has not published (a reload mounts only the
 * shown tab). An app page is its folder; a canvas page its canvas (no clone
 * dir known); an explorer page its folder, or — the last segment read as a
 * file, `tabKind`'s guess — the file in its parent folder. Anything else has
 * no place (null).
 */
export function placeOfUrl(url: string): RailPlace | null {
  const { path } = splitUrl(url);
  const app = appPathFromPath(path);
  if (app !== null) return { kind: "folder", dir: app, word: "App" };
  const canvas = /^\/canvases\/(unstable|prod|custom)\/([A-Za-z0-9_]+)$/.exec(path);
  if (canvas) return { kind: "canvas", env: canvas[1] as WorkbenchEnv, name: canvas[2], dir: null };
  if (!path.startsWith(EXPLORER_VIEW + "/") || isLayoutSentinel(path)) return null;
  const fsPath = fsPathOfViewPath(path);
  if (fsPath === null || fsPath.startsWith("s3://")) return null;
  if (tabKind(url) !== "file") return { kind: "folder", dir: fsPath };
  const cut = fsPath.lastIndexOf("/");
  return { kind: "file", file: fsPath, dir: cut > 0 ? fsPath.slice(0, cut) : null };
}

/**
 * Where `url` (same-origin path+query) opens in a workspace:
 *   - a chat door (`/explorer/view/<p>?…_side=claude…`, what every taskHref /
 *     chatUrl / chatPaneUrl builds) → the panel's chat on `<p>` with the door
 *     params; `_side=git` → the panel's Git on `<p>`;
 *   - `/tasks` (with `peek=`) → Tasks; `/bots…` → Bots;
 *   - a canvas page naming a `session_id` (a canvas task's door) → the panel,
 *     once the host finds the task by that session;
 *   - anything else → the left.
 */
export function classifyNav(url: string): NavPlace {
  const { path, query } = splitUrl(url);
  const params = new URLSearchParams(query);
  const fsPath = fsPathOfViewPath(path);
  if (fsPath !== null) {
    // `_mode=claude|git` is the same companion spelled as a content mode (an
    // old link, a bookmark): in a workspace those live in the panel.
    const side = [params.get("_side"), params.get("_mode")].find((v) => v === "claude" || v === "git");
    if (side === "claude") return { side: "right", view: { kind: "chat", file: fsPath, query: chatDoorQuery(query) } };
    if (side === "git") return { side: "right", view: { kind: "git", path: fsPath } };
    return { side: "left" };
  }
  if (path === "/tasks") return { side: "right", view: { kind: "tasks", peek: params.get("peek") || null } };
  if (isBotsUrl(path)) return { side: "right", view: { kind: "bots", url: joinUrl({ path, query, hash: "" }) } };
  if (path.startsWith("/canvases/") && path.split("/").filter(Boolean).length === 3) {
    const sid = params.get("session_id");
    if (sid) return { side: "session", sessionId: sid };
  }
  return { side: "left" };
}

/**
 * A TAB BOOTING ON A COMPANION PANE (WS-25): `/explorer/view/<p>?_side=claude
 * &session_id=…` (or `_mode=claude|git`, `_side=git`) names a pane the page
 * draws no more in a workspace. `right` is what the panel should show, and
 * `keep` the tab's URL without the pane and its door params; a URL naming no
 * pane is kept as it is (`right` null).
 */
export function splitArrival(url: string): { keep: string; right: RightView | null } {
  const place = classifyNav(url);
  const parts = splitUrl(url);
  if (place.side !== "right" || (place.view.kind !== "chat" && place.view.kind !== "git") || !fsPathOfViewPath(parts.path)) {
    return { keep: url, right: null };
  }
  const drop = (seg: string): boolean => {
    const eq = seg.indexOf("=");
    const name = eq === -1 ? seg : seg.slice(0, eq);
    const value = eq === -1 ? "" : seg.slice(eq + 1);
    if (name === "_side" || name === "_mode") return value === "claude" || value === "git";
    return (CHAT_DOOR_PARAMS as readonly string[]).includes(name);
  };
  const query = querySegments(parts.query).filter((s) => !drop(s)).join("&");
  return { keep: joinUrl({ ...parts, query }), right: place.view };
}
