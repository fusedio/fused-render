// Shared `_layout` codec for the URL-is-state layout modes (SPEC §14/§15, D45/
// D47). Both views/Panel.tsx (split panes) and views/Tabs.tsx (tabs) encode a
// set of /embed locations into the reserved `_layout` query param and parse it
// back; Breadcrumb.tsx reuses the segment encoder for its Split button. Keeping
// one codec here means one escaping story and hand-convertible panel<->tab URLs.
// Imports router.ts only (for the shared embed prefix); everything here is a
// helper both modes share — codec, embed-frame URL access, and the
// fused:urlchange hook — with no view or chrome dependencies.
//
// Panes/tabs are /embed/<path> iframes (D39): a full chrome-free shell, so each
// can browse dirs, open previews and use templates for free.
import { EMBED_PREFIX, rootedFsPath } from "./router";

// --- Tree codec (`_layout` param) -----------------------------------------
// `,` = row (side by side), `;` = column (stacked), `(…)` groups for nesting.
// A leaf = the fs path + optional segment-local query, e.g.
// `/data/a.parquet?_mode=source&sort=name`. The structural chars `, ; ( ) %`
// (and `?` inside the path, so the first `?` always separates path from query)
// are percent-encoded within a segment so the delimiters stay unambiguous.
// The reference grid-viewer's splitDepthAware() is the model; per-segment
// escaping is the addition it lacks.

export interface LayoutLeaf {
  type: "leaf";
  id: number;
  path: string;
  query: string;
}

export interface LayoutSplit {
  type: "split";
  dir: "row" | "col";
  children: LayoutNode[];
  // Assigned by Panel.tsx for React keys (parseLayout leaves it unset).
  id?: number;
}

export type LayoutNode = LayoutLeaf | LayoutSplit;

// Monotonic node ids — unique per session, never reset (only one layout mode is
// live at a time, so uniqueness is all that matters; no reset needed).
let idSeq = 0;

export function leaf(path: string, query?: string): LayoutLeaf {
  return { type: "leaf", id: ++idSeq, path, query: query || "" };
}

// Escape a path component: % first (so escapes aren't re-escaped), then the
// codec delimiters, plus `?` so the path can never contain the path/query
// separator.
function encPath(s: string): string {
  return s
    .replace(/%/g, "%25")
    .replace(/,/g, "%2C")
    .replace(/;/g, "%3B")
    .replace(/\(/g, "%28")
    .replace(/\)/g, "%29")
    .replace(/\?/g, "%3F");
}

// Escape a query segment: same as encPath but keep `?` literal (the leading
// `?` is the separator; any later `?` in a value is harmless once we split on
// the first one).
function encQuery(s: string): string {
  return s
    .replace(/%/g, "%25")
    .replace(/,/g, "%2C")
    .replace(/;/g, "%3B")
    .replace(/\(/g, "%28")
    .replace(/\)/g, "%29");
}

// Reverse either escaping in one left-to-right pass. %25 decodes to `%` and
// scanning continues past it, so a literal `%2C` (escaped to `%252C`) survives
// while a structural `%2C` (an escaped comma) becomes `,`.
function decSeg(s: string): string {
  return s.replace(/%(25|2C|3B|28|29|3F)/g, (_, hex) => String.fromCharCode(parseInt(hex, 16)));
}

// Encode one segment (fs path + optional query, query includes its `?`).
// Exported so the breadcrumb's Split button and Tabs.tsx can turn a location
// into a segment without duplicating the codec.
export function encodePaneSegment(path: string, query?: string): string {
  return encPath(path) + encQuery(query || "");
}

export function encodeNode(node: LayoutNode, parentDir?: "row" | "col"): string {
  if (node.type === "leaf") return encodePaneSegment(node.path, node.query);
  const sep = node.dir === "row" ? "," : ";";
  const s = node.children.map((c) => encodeNode(c, node.dir)).join(sep);
  // Parenthesize when nesting would be misread (a column inside a row, or a
  // column inside a column).
  return node.dir === "col" && parentDir ? "(" + s + ")" : s;
}

// Split on `sep` only at bracket depth 0.
function splitDepthAware(str: string, sep: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let cur = "";
  for (const ch of str) {
    if (ch === "(") depth++;
    else if (ch === ")") depth--;
    if (ch === sep && depth === 0) {
      out.push(cur);
      cur = "";
    } else {
      cur += ch;
    }
  }
  out.push(cur);
  return out;
}

function parseLeaf(seg: string): LayoutLeaf {
  // First `?` separates path from query (path escaping guarantees no earlier
  // `?`). Both halves are un-escaped; the query keeps its leading `?`.
  const q = seg.indexOf("?");
  if (q === -1) return leaf(decSeg(seg), "");
  return leaf(decSeg(seg.slice(0, q)), "?" + decSeg(seg.slice(q + 1)));
}

export function parseLayout(str: string): LayoutNode {
  const rows = splitDepthAware(str, ";").map((row): LayoutNode => {
    const cells = splitDepthAware(row, ",").map((cell): LayoutNode => {
      cell = cell.trim();
      if (cell.startsWith("(") && cell.endsWith(")")) return parseLayout(cell.slice(1, -1));
      return parseLeaf(cell);
    });
    return cells.length === 1 ? cells[0] : { type: "split", dir: "row", children: cells };
  });
  return rows.length === 1 ? rows[0] : { type: "split", dir: "col", children: rows };
}

// Flatten any tree to its leaves in document order. Tab mode (TM-2) produces a
// flat `,` row but defensively flattens any nested `;`/`()` structure it is
// handed, each leaf becoming a tab.
export function flattenToLeaves(node: LayoutNode): LayoutLeaf[] {
  if (node.type === "leaf") return [node];
  const out: LayoutLeaf[] = [];
  for (const c of node.children) out.push(...flattenToLeaves(c));
  return out;
}

// --- `_layout` <-> shell URL ----------------------------------------------
// Grammar (D51): the entire `_layout` value is parenthesized and emitted LAST —
// `?global=1&_layout=(/a.html?x=1&y=2,/b.html)`. The parens delimit scope
// visually (inside = iframe-local, outside = global) and structurally: `&` is
// LITERAL inside them, so plain URLSearchParams cannot parse a layout URL —
// every read of a shell query goes through splitShellSearch() below. The wrap
// is codec-transparent (parseLayout treats `(A,B)` as `A,B`). Strict read: an
// unwrapped `_layout` value is not this grammar and is treated as absent (no
// lenient fallback; the key is dropped on the next sync).
//
// The codec string keeps `, ; ( ) / ? & =` literal for a readable address bar
// (SPEC §14 example). Only `%` (escape ambiguity), `#` (fragment) and space
// (invalid raw / browser re-encoding churn) are escaped when placing it inside
// the parens; one decodeURIComponent pass in splitShellSearch reverses this —
// literal parens inside segments are codec-escaped (`%28` → `%2528` here), so
// the only literal parens in the span are structural and balanced.
function urlSafeLayout(s: string): string {
  return s.replace(/%/g, "%25").replace(/#/g, "%23").replace(/ /g, "%20");
}

// Build <sentinel>?...&_layout=(...) : any top-level params first (hand-typed
// globals only, D72 — the shells never promote params there), the
// parenthesized layout always last. `merged` is an iterable of [k, v]
// entries; `_layout` is dropped from it so callers can pass the full current
// query (also discards any old-grammar unwrapped value, see splitShellSearch).
export function buildSentinelUrl(
  sentinelPath: string,
  codecStr: string,
  merged?: Iterable<[string, string]> | null,
): string {
  const parts: string[] = [];
  if (merged) {
    for (const [k, v] of merged) {
      if (k === "_layout") continue;
      parts.push(encodeURIComponent(k) + "=" + encodeURIComponent(v));
    }
  }
  parts.push("_layout=(" + urlSafeLayout(codecStr) + ")");
  return sentinelPath + "?" + parts.join("&");
}

export interface ShellSearch {
  layout: string | null;
  params: URLSearchParams;
}

// Parse a shell query string under the D51 grammar: extract the parenthesized
// `_layout=(...)` span by balanced-paren scan, return the decoded codec string
// plus the remaining params. `layout` is null when the param is absent,
// unwrapped (old grammar — strict read, treated as absent), unbalanced
// (paste-truncated URL, accepted breakage) or undecodable; the broken span is
// still excluded from `params` so it can't leak junk keys. runtime.js carries
// a small duplicate of this scan (it is injected standalone, no imports).
export function splitShellSearch(search: string): ShellSearch {
  const s = (search || "").replace(/^\?/, "");
  const m = /(^|&)_layout=/.exec(s);
  if (!m) return { layout: null, params: new URLSearchParams(s) };
  const valStart = m.index + m[1].length + "_layout=".length;
  if (s[valStart] !== "(") {
    // Unwrapped value: not this grammar. URLSearchParams handles it as an
    // ordinary param; buildSentinelUrl drops the key on the next sync.
    return { layout: null, params: new URLSearchParams(s) };
  }
  let i = valStart + 1;
  let depth = 1;
  while (i < s.length && depth > 0) {
    if (s[i] === "(") depth++;
    else if (s[i] === ")") depth--;
    i++;
  }
  // Span runs to the matching `)`, or to end-of-string when unbalanced (the
  // truncated span is dropped from params either way).
  const rest = (s.slice(0, m.index) + s.slice(i)).replace(/^&|&$/g, "");
  const params = new URLSearchParams(rest);
  if (depth !== 0) return { layout: null, params };
  let layout: string | null = null;
  try {
    layout = decodeURIComponent(s.slice(valStart + 1, i - 1));
  } catch {
    /* malformed percent escape → invalid layout */
  }
  return { layout, params };
}

// --- Embed URL helpers -----------------------------------------------------
export function embedSrc(path: string, query?: string): string {
  const encoded = path
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return EMBED_PREFIX + encoded + (query || "");
}

export interface UrlChangeHook {
  win: Window;
  handler: () => void;
}

// Attach a fused:urlchange listener to an embed iframe's current document.
// contentWindow is a WindowProxy whose identity never changes, but the
// underlying Window (and any listeners on it) is replaced on every full
// navigation — so the attached-marker must live as an expando on the window
// itself: it dies with the document, making re-attachment (callers re-run this
// from the iframe `load` handler) exactly track the listener's actual lifetime.
// Returns a hook {win, handler} for detachEmbedUrlChange, or null when this
// document is already hooked (keep the caller's existing hook) or unreachable.
// One expando serves both modes: a panel and a tab shell never hook the same
// window — each hooks only its direct child iframes.
export function attachEmbedUrlChange(
  iframe: HTMLIFrameElement,
  handler: () => void,
): UrlChangeHook | null {
  let win: Window;
  try {
    if (!iframe.contentWindow) return null;
    win = iframe.contentWindow;
    if (win._fusedUrlHooked) return null;
    win._fusedUrlHooked = true;
  } catch {
    return null;
  }
  win.addEventListener("fused:urlchange", handler);
  return { win, handler };
}

// Null-safe detach of a hook returned by attachEmbedUrlChange.
export function detachEmbedUrlChange(hook: UrlChangeHook | null): void {
  if (!hook) return;
  try {
    hook.win.removeEventListener("fused:urlchange", hook.handler);
  } catch {
    /* window gone */
  }
}

export interface EmbedLoc {
  path: string;
  query: string;
}

// Read a mounted iframe's live location (D39): fs path + query, so duplicates/
// crumbs/sync/labels follow in-pane navigation. Returns null when the iframe is
// cross-origin (shouldn't happen) or not under /embed/.
export function readEmbedLoc(iframe: HTMLIFrameElement): EmbedLoc | null {
  try {
    const loc = iframe.contentWindow?.location;
    if (!loc) return null;
    const p = loc.pathname;
    if (p && p.startsWith(EMBED_PREFIX)) {
      const rest = p.slice(EMBED_PREFIX.length);
      // rootedFsPath keeps the shell's canonical form: leading slash for
      // POSIX, none for Windows drive paths ("C:/…") — a bare "/"-prefixed
      // drive path would never prefix-match home or other fs paths.
      const path = rootedFsPath(
        rest
          .split("/")
          .filter((s) => s.length > 0)
          .map(decodeURIComponent)
          .join("/"),
      );
      return { path, query: loc.search || "" };
    }
  } catch {
    // Cross-origin (shouldn't happen — same-origin) — ignore.
  }
  return null;
}
