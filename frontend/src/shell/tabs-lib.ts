// Shell tabs, the pure half (TabHost.tsx is the React half): the host URL's
// `_tabs` codec, the embed marker a tab's iframe src carries, and reading a
// tab iframe's live location.
//
// DOM-FREE AT IMPORT, so bun can test it — which is why the three route
// constants below are RESTATED rather than imported: router.ts touches
// `window`/`location` at module init (IS_NATIVE_WINDOW reads `window.top`),
// and bun has no window. Same posture, same reason, as current-apps-lib's
// restated segment codec. Keep these in step with router.ts.
const VIEW_PREFIX = "/explorer/view/";
const EMBED_PREFIX = "/explorer/embed/";
const EMBED_PARAM = "embed";

// The one reserved host param. Underscore-prefixed like every shell-owned
// param (`_layout`, `_side`), so a template's `fused.params` never sees it.
export const TABS_PARAM = "_tabs";

// The active tab's slot in `_tabs`. A literal `.` can never be an item —
// every real item is `encodeURIComponent(path+query)` and so starts `%2F`.
const ACTIVE_SLOT = ".";

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

// Every query edit here is TEXTUAL, segment by segment, never a
// URLSearchParams round-trip: that re-encodes every other param (`_layout=(…)`
// is not even URLSearchParams grammar, layout-codec D51), so a host URL would
// come back byte-different from the one it was built from and the mirror's
// "only when changed" guard (Tabs.tsx TM-7) would write on every sync.
function querySegments(query: string): string[] {
  return query ? query.split("&").filter((s) => s.length > 0) : [];
}

const MARKER_SEGMENT = EMBED_PARAM + "=1";

function hasMarker(query: string): boolean {
  return querySegments(query).includes(MARKER_SEGMENT);
}

// The SHELL'S OWN ROUTES: the paths the server answers with this SPA, and
// the only ones whose chrome the `?embed=1` spelling (IS_QUERY_EMBED) drops.
// A closed list, like router.ts's JOB_PAGE_ROUTES, because the alternative
// ("anything not explorer") is wrong for the one other thing a tab opens: a
// `/render?path=…` runtime page (a bot's app, an app's run target) is not the
// shell at all. It is chrome-free already, and a stray `embed` key on its
// query would surface in the page's `fused.params`. Prefix entries cover the
// routes whose sub-paths are the page's own state (App.tsx's dispatch).
// Keep in step with App.tsx when a route is added.
const SHELL_ROUTES = new Set([
  "/",
  "/home",
  "/apps",
  "/tasks",
  "/preferences",
  "/templates",
  "/monitor",
  "/canvases",
  "/bots",
  "/ai-models",
  "/explorer",
  "/claude-config",
  "/claude-md",
  // NOT `/onboarding`: the setup wizard is the whole window, never a tab —
  // the host sends every hop to it to a full load of the window (TabHost).
]);
const SHELL_ROUTE_PREFIXES = ["/apps/", "/canvases/", "/bots/", "/ai-models/"];

function isExplorerPath(path: string): boolean {
  return (
    path.startsWith(VIEW_PREFIX) ||
    path.startsWith(EMBED_PREFIX) ||
    path === "/explorer/view" ||
    path === "/explorer/embed"
  );
}

function isShellRoute(path: string): boolean {
  return SHELL_ROUTES.has(path) || SHELL_ROUTE_PREFIXES.some((p) => path.startsWith(p));
}

/**
 * Is `url` a page of this SPA: a document whose router can be asked to
 * navigate in place (router.ts `fused:navigate-url`), and a location the host
 * itself may sit on? Shell routes and the explorer; not `/render` or anything
 * else the server answers with a page of its own.
 */
export function isSpaUrl(url: string): boolean {
  const { path } = splitUrl(url);
  return path === "" || isExplorerPath(path) || isShellRoute(path);
}

/**
 * The iframe src a tab loads for a same-origin `url` (path+query, no marker).
 *
 * A tab is a whole page of this same shell, chrome-free: every route already
 * drops its chrome under IS_EMBED (router.ts), so the only job here is to load
 * each route in its embed spelling — the explorer under its own `/embed/`
 * prefix, every other SHELL route with the `?embed=1` query spelling
 * (IS_QUERY_EMBED). Anything that is not the shell (`/render?path=…`) passes
 * through verbatim. `/` stays `/`: the front door is the TAB's call, not this
 * function's — App's own rewrite sends it to Home, or to Bots under the Fused
 * Bot flavor, keeping the query (and so the marker) as it goes. Idempotent,
 * so a url that already carries its marker passes through.
 */
export function tabSrc(url: string): string {
  const parts = splitUrl(url);
  if (parts.path.startsWith(VIEW_PREFIX)) {
    return joinUrl({ ...parts, path: EMBED_PREFIX + parts.path.slice(VIEW_PREFIX.length) });
  }
  if (parts.path === "/explorer/view") return joinUrl({ ...parts, path: "/explorer/embed" });
  if (parts.path.startsWith(EMBED_PREFIX) || parts.path === "/explorer/embed") return url;
  if (parts.path !== "" && !isShellRoute(parts.path)) return url;
  const path = parts.path || "/";
  const query = hasMarker(parts.query)
    ? parts.query
    : querySegments(parts.query).concat(MARKER_SEGMENT).join("&");
  return joinUrl({ path, query, hash: parts.hash });
}

/**
 * The inverse of `tabSrc`'s query half: `embed=1` removed from a SHELL route,
 * everything else byte-identical (a non-shell page's own `embed` param is its
 * own). The `/explorer/embed/` PREFIX stays — a tab opened on an embed URL is
 * an embed tab, and turning it into a view here would be a second answer to
 * "where is this tab" (the host's own location makes its own choice, see
 * hostLocation).
 */
export function stripTabMarker(url: string): string {
  const parts = splitUrl(url);
  if (!isShellRoute(parts.path) || !hasMarker(parts.query)) return url;
  return joinUrl({
    ...parts,
    query: querySegments(parts.query).filter((s) => s !== MARKER_SEGMENT).join("&"),
  });
}

/**
 * The URL the HOST document may sit on for an active tab at `url`.
 *
 * The host is a top-level shell, and its own load decides whether it is one:
 * a host reloaded on `/explorer/embed/<p>` boots as a chrome-free embed
 * (IS_EMBED is read once at load) and never becomes a tab host again — the
 * window would come back as a bare page with no strip and no sidebar. So the
 * host mirrors an embed tab under the VIEW prefix: the same folder or file,
 * and `tabSrc` maps it straight back to the embed spelling for the frame.
 * Marker stripped too, for the same reason (`?embed=1` is IS_QUERY_EMBED).
 */
export function hostLocation(url: string): string {
  const parts = splitUrl(stripTabMarker(url));
  if (parts.path.startsWith(EMBED_PREFIX)) {
    return joinUrl({ ...parts, path: VIEW_PREFIX + parts.path.slice(EMBED_PREFIX.length) });
  }
  if (parts.path === "/explorer/embed") return joinUrl({ ...parts, path: "/explorer/view" });
  return joinUrl(parts);
}

/**
 * Two tab urls name the same PAGE — what `open(url, {focusExisting})` asks.
 * Pathname only (a tab that has since changed its sort is still that tab),
 * compared in the frame's own spelling so `/explorer/view/a` and
 * `/explorer/embed/a` — the same page inside a tab — match, and `/` matches
 * `/home`. Where the QUERY is the page, it is part of the identity too:
 *   - `/render` is one path for every app (two bots' apps are two pages);
 *   - the explorer's `_panel`/`_tab` sentinels name their whole layout in it;
 *   - `/tasks?project=<dir>` is one app's tasks, not everyone's (its other
 *     params, `?view=`/`?peek=`, are view state and do not count).
 */
export function samePage(a: string, b: string): boolean {
  const pa = splitUrl(stripTabMarker(tabSrc(a)));
  const pb = splitUrl(stripTabMarker(tabSrc(b)));
  // `/` is the front door, which is Home (outside the Fused Bot flavor).
  const home = (p: string) => (p === "/" ? "/home" : p);
  if (home(pa.path) !== home(pb.path)) return false;
  if (!isSpaUrl(a) || isLayoutSentinel(pa.path)) return pa.query === pb.query;
  if (pa.path === "/tasks") return queryParam(pa.query, "project") === queryParam(pb.query, "project");
  return true;
}

function isLayoutSentinel(path: string): boolean {
  return /^\/explorer\/(view|embed)\/_(panel|tab)$/.test(path);
}

function queryParam(query: string, name: string): string | null {
  try {
    return new URLSearchParams(query).get(name);
  } catch {
    return null;
  }
}

// Where the host sits when the ACTIVE tab is not a page of this SPA: the
// server answers `/render?…` with the runtime page itself, so a host reloaded
// there would come back as a bare app with no strip. The host sits on Home
// instead and names the active tab explicitly with `!` (below).
const NON_SPA_HOST = "/home";
const EXPLICIT_ACTIVE = "!";

/**
 * The `_tabs` value for a strip of `urls` with `activeIndex` active: every
 * non-active item `encodeURIComponent`-ed, comma-joined. The active slot is a
 * literal `.` (its url IS the host location — no reason to say it twice)
 * unless the active tab is not an SPA page, which the host cannot sit on:
 * then the slot is `!` + the encoded url. `null` for a single SPA tab: one
 * tab is just a window, and its URL carries no tabs param at all. Items
 * always start `%2F`, so neither marker can collide with one.
 */
export function encodeTabsParam(urls: string[], activeIndex: number): string | null {
  const activeIsSpa = isSpaUrl(urls[activeIndex] ?? "/");
  if (urls.length <= 1 && activeIsSpa) return null;
  return urls
    .map((u, i) =>
      i !== activeIndex
        ? encodeURIComponent(u)
        : activeIsSpa
          ? ACTIVE_SLOT
          : EXPLICIT_ACTIVE + encodeURIComponent(u),
    )
    .join(",");
}

/**
 * The whole host URL: the active tab's url (Home, for a non-SPA active tab)
 * with `_tabs` appended LAST. `urls` are tab urls as `hostLocation` spells
 * them.
 */
export function buildHostUrl(urls: string[], activeIndex: number): string {
  const active = urls[activeIndex] ?? urls[0] ?? "/";
  const value = encodeTabsParam(urls, activeIndex);
  if (value === null) return active;
  const parts = splitUrl(isSpaUrl(active) ? active : NON_SPA_HOST);
  return joinUrl({
    ...parts,
    query: querySegments(parts.query).concat(TABS_PARAM + "=" + value).join("&"),
  });
}

/**
 * Parse a host `pathname+search` back into the strip. A url with no `_tabs`
 * is one tab (a fresh window on any route). Malformed input degrades instead
 * of throwing: an undecodable or foreign item is dropped; a list with no
 * active slot puts the location itself first as the active tab; a second
 * active slot is ignored (`.`) or read as an ordinary tab (`!`).
 */
export function parseHostUrl(url: string): { tabs: string[]; active: number } {
  const parts = splitUrl(url);
  const segs = querySegments(parts.query);
  const at = segs.findIndex((s) => s.startsWith(TABS_PARAM + "="));
  if (at === -1) return { tabs: [url], active: 0 };
  const raw = segs[at].slice(TABS_PARAM.length + 1);
  const own = joinUrl({ ...parts, query: segs.filter((_, i) => i !== at).join("&") });
  const tabs: string[] = [];
  let active = -1;
  for (const item of raw.split(",")) {
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
    // Same-origin paths only: an item is put back into an iframe src, so a
    // hand-edited `https://…` or `javascript:` must not become a tab.
    if (!decoded.startsWith("/") || decoded.startsWith("//")) continue;
    if (explicit && active === -1) active = tabs.length;
    tabs.push(decoded);
  }
  if (active === -1) {
    tabs.unshift(own);
    active = 0;
  }
  return { tabs, active };
}

/**
 * A tab iframe's live `pathname+search`, or null (not loaded, cross-origin,
 * gone). The generalisation of layout-codec's readEmbedLoc to any route: a tab
 * is not always under `/explorer/embed/`, and the host wants the URL as a URL,
 * not decoded into an fs path.
 */
export function readTabLoc(iframe: HTMLIFrameElement | null): string | null {
  try {
    const loc = iframe?.contentWindow?.location;
    if (!loc || loc.href === "about:blank") return null;
    return loc.pathname + loc.search;
  } catch {
    return null;
  }
}

/**
 * A tab's strip label. The frame's own `document.title` wins (every route
 * sets one, `useDocumentTitle`), minus the ` – <brand>` suffix the whole app
 * appends — the strip is inside the brand already. Fallbacks: "Home" for
 * Home, else the decoded last path segment.
 */
export function tabLabel(url: string, docTitle: string | null | undefined, brand: string): string {
  const t = (docTitle || "").trim();
  if (t) {
    const suffix = " – " + brand;
    const bare = t.endsWith(suffix) ? t.slice(0, -suffix.length) : t;
    if (bare && bare !== brand) return bare;
  }
  const { path, query } = splitUrl(stripTabMarker(url));
  if (path === "/" || path === "/home") return "Home";
  // A runtime page names its file in `?path=`; "render" would name nothing.
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

/** Index after a step of `delta` through `count` tabs, wrapping. */
export function stepIndex(current: number, delta: number, count: number): number {
  if (count <= 0) return 0;
  return (((current + delta) % count) + count) % count;
}

/**
 * The tab to activate after closing index `closed` of `count`: the one that
 * slides into its slot, else (it was the last) the new last.
 */
export function neighborAfterClose(closed: number, count: number): number {
  const left = count - 1;
  if (left <= 0) return -1;
  return Math.min(closed, left - 1);
}

/** Move `from` to `to` in an order array (a strip reorder). Pure. */
export function moveItem<T>(arr: readonly T[], from: number, to: number): T[] {
  const out = arr.slice();
  if (from < 0 || from >= out.length) return out;
  const [item] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(to, out.length)), 0, item);
  return out;
}
