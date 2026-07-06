// Shared `_layout` codec for the URL-is-state layout modes (SPEC §14/§15, D45/
// D47). Both views/panel.js (split panes) and views/tabs.js (tabs) encode a
// set of /embed locations into the reserved `_layout` query param and parse it
// back; breadcrumb.js reuses the segment encoder for its Split button. Keeping
// one codec here means one escaping story and hand-convertible panel<->tab URLs.
// Pure helpers only — imports nothing (respects the one-way dep rule).

// Panes/tabs are /embed/<path> iframes (D39): a full chrome-free shell, so each
// can browse dirs, open previews and use templates for free.
export const EMBED_PREFIX = "/embed/";

// --- Tree codec (`_layout` param) -----------------------------------------
// `,` = row (side by side), `;` = column (stacked), `(…)` groups for nesting.
// A leaf = the fs path + optional segment-local query, e.g.
// `/data/a.parquet?_mode=source&sort=name`. The structural chars `, ; ( ) %`
// (and `?` inside the path, so the first `?` always separates path from query)
// are percent-encoded within a segment so the delimiters stay unambiguous.
// The reference grid-viewer's splitDepthAware() is the model; per-segment
// escaping is the addition it lacks.

// Monotonic node ids — unique per session, never reset (only one layout mode is
// live at a time, so uniqueness is all that matters; no reset needed).
let idSeq = 0;

export function leaf(path, query) {
  return { type: "leaf", id: ++idSeq, path, query: query || "" };
}

// Escape a path component: % first (so escapes aren't re-escaped), then the
// codec delimiters, plus `?` so the path can never contain the path/query
// separator.
function encPath(s) {
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
function encQuery(s) {
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
function decSeg(s) {
  return s.replace(/%(25|2C|3B|28|29|3F)/g, (_, hex) =>
    String.fromCharCode(parseInt(hex, 16))
  );
}

// Encode one segment (fs path + optional query, query includes its `?`).
// Exported so the breadcrumb's Split button and tabs.js can turn a location
// into a segment without duplicating the codec.
export function encodePaneSegment(path, query) {
  return encPath(path) + encQuery(query || "");
}

export function encodeNode(node, parentDir) {
  if (node.type === "leaf") return encodePaneSegment(node.path, node.query);
  const sep = node.dir === "row" ? "," : ";";
  const s = node.children.map((c) => encodeNode(c, node.dir)).join(sep);
  // Parenthesize when nesting would be misread (a column inside a row, or a
  // column inside a column).
  return node.dir === "col" && parentDir ? "(" + s + ")" : s;
}

// Split on `sep` only at bracket depth 0.
function splitDepthAware(str, sep) {
  const out = [];
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

function parseLeaf(seg) {
  // First `?` separates path from query (path escaping guarantees no earlier
  // `?`). Both halves are un-escaped; the query keeps its leading `?`.
  const q = seg.indexOf("?");
  if (q === -1) return leaf(decSeg(seg), "");
  return leaf(decSeg(seg.slice(0, q)), "?" + decSeg(seg.slice(q + 1)));
}

export function parseLayout(str) {
  const rows = splitDepthAware(str, ";").map((row) => {
    const cells = splitDepthAware(row, ",").map((cell) => {
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
export function flattenToLeaves(node) {
  if (node.type === "leaf") return [node];
  const out = [];
  for (const c of node.children) out.push(...flattenToLeaves(c));
  return out;
}

// --- `_layout` <-> shell URL ----------------------------------------------
// The codec string keeps `, ; ( ) /` literal for a readable address bar
// (SPEC §14 example). Only the chars that would break parsing of a query-param
// value are escaped here; URLSearchParams.get('_layout') reverses this exactly.
function urlSafeLayout(s) {
  return s
    .replace(/%/g, "%25")
    .replace(/&/g, "%26")
    .replace(/#/g, "%23")
    .replace(/\+/g, "%2B")
    .replace(/ /g, "%20");
}

// Build <sentinel>?_layout=... : the encoded tree/list plus the merged
// (top-level) param pool. `merged` is an iterable of [k, v] entries; `_layout`
// is dropped from it so callers can pass the full current query.
export function buildSentinelUrl(sentinelPath, codecStr, merged) {
  let s = sentinelPath + "?_layout=" + urlSafeLayout(codecStr);
  if (merged) {
    for (const [k, v] of merged) {
      if (k === "_layout") continue;
      s += "&" + encodeURIComponent(k) + "=" + encodeURIComponent(v);
    }
  }
  return s;
}

// --- Embed URL helpers -----------------------------------------------------
export function embedSrc(path, query) {
  const encoded = path
    .replace(/^\/+/, "")
    .split("/")
    .filter((s) => s.length > 0)
    .map(encodeURIComponent)
    .join("/");
  return EMBED_PREFIX + encoded + (query || "");
}

// Read a mounted iframe's live location (D39): fs path + query, so duplicates/
// crumbs/sync/labels follow in-pane navigation. Returns null when the iframe is
// cross-origin (shouldn't happen) or not under /embed/.
export function readEmbedLoc(iframe) {
  try {
    const loc = iframe.contentWindow.location;
    const p = loc.pathname;
    if (p && p.startsWith(EMBED_PREFIX)) {
      const rest = p.slice(EMBED_PREFIX.length);
      const path =
        "/" +
        rest
          .split("/")
          .filter((s) => s.length > 0)
          .map(decodeURIComponent)
          .join("/");
      return { path, query: loc.search || "" };
    }
  } catch (e) {
    // Cross-origin (shouldn't happen — same-origin) — ignore.
  }
  return null;
}
