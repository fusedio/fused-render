// Widens a zero-hit PATTERN (glob) query to its recursively-broadened form —
// derived from the query's own text, not from a special-cased example.
//
// `resolve_query` (fused_render/index/query.py) already widens a slash-free
// glob for free: "*.js" resolves to "**/*.js" server-side, matching any
// depth. A query that DOES carry a slash keeps whatever depth its own
// segments spell out — "/home/iamsdas/*.js" only reaches direct children of
// /home/iamsdas. This inserts the same "**/" the server would have added,
// immediately before the query's own LAST segment, so the pattern searches
// every depth under whatever base it already had: "/home/iamsdas/*.js"
// becomes "/home/iamsdas/**/*.js". Everything before that segment — the
// base, any earlier glob segment — is left exactly as written; only the
// final component's own reach changes.
//
// Returns null when there is nothing left to widen:
//   - the query isn't a glob at all (`"*" in raw` is `resolve_query`'s own
//     trigger, mirrored here, per path-shaped-query.ts's own comment on the
//     same rule) — a substring query is untouched;
//   - the query is already maximally broad: no "/" at all (the server's own
//     implicit "**/" prefix already covers every depth), or the segment
//     immediately before the last one is already "**" — this is exactly the
//     widened form of itself, so offering to widen it again would rerun the
//     identical search.
export function broadenGlobPattern(query: string): string | null {
  if (!query.includes("*")) return null;
  if (!query.includes("/")) return null;
  const segments = query.split("/");
  const last = segments[segments.length - 1];
  const beforeLast = segments[segments.length - 2];
  if (beforeLast === "**") return null;
  const head = segments.slice(0, -1).join("/");
  return `${head}/**/${last}`;
}
