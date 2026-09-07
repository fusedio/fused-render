// The `_snapshot` shell URL param's carry rule and the small piece of state
// `router.ts::navigate` needs to apply it.
//
// LIVES IN platform/, NOT apps/explorer/, though the feature is explorer-only
// and apps/explorer/lib/snapshot-param.ts is where its public surface is
// documented and re-exported from. The reason is the same one appEntry.ts,
// appAnnotation.ts and dismissOnOutside.ts already state for themselves:
// platform/ never imports from apps/ (no existing platform module does, and
// router.ts's own comments about /apps/<tag>/<name> never resolve to an
// import) — it is the boundary that keeps a platform-level concern like
// routing from depending on any one app's internals. `navigate` is exactly
// such a concern, so the logic it calls has to sit beside it; apps/explorer's
// copy of this module is a thin re-export for Preview.tsx and Listing.tsx, the
// two callers that actually own resolving and setting it.
//
// THE CARRY RULE. `_snapshot=<sha>` survives a navigation exactly when the
// DESTINATION is still inside the app folder the sha was resolved against —
// the same file, a sibling file, a subfolder, all carry; a breadcrumb hop out
// of the app folder, or a hop into an unrelated app, both drop it. A sha from
// one app's history says nothing about another app, or about the workspace
// above it.

// What one `/api/git/snapshot` resolution produced: the sha it was asked
// about (not part of that response — the caller already knows it, having
// asked with it) and the two paths the response named. `dir` is what
// Listing.tsx rewrites a directory listing's fetch target to (mirroring
// static/runtime.js's own `rewritePath`, so the explorer's listing and every
// template under the app agree on what a snapshotted read means); `app_dir`
// is the live folder `carries` checks navigations against.
export interface ResolvedSnapshot {
  sha: string;
  dir: string;
  app_dir: string;
}

// The CURRENT `_snapshot`'s resolution, or null before one has landed
// (including "there is no `_snapshot` right now"). NOT part of the URL (only
// the sha is): only one sha can be "the" snapshot for this shell session at a
// time, and what it resolved to is a fact about that resolution, not a second
// piece of state a hand-typed or bookmarked URL has to agree with.
//
// A plain module-level variable, not a param and not sessionStorage: it needs
// no persistence of its own. A reload re-resolves it from the URL's
// `_snapshot` plus the current path exactly as the first load did (whichever
// code resolves `/api/git/snapshot` for this document sets it, both on
// selecting a commit and on a fresh load that already carries the param) —
// which is what makes a copied link work in a browser session that never ran
// this JS before.
let resolvedSnapshot: ResolvedSnapshot | null = null;

export function setResolvedSnapshot(snap: ResolvedSnapshot | null): void {
  resolvedSnapshot = snap;
}

export function getResolvedSnapshot(): ResolvedSnapshot | null {
  return resolvedSnapshot;
}

// The narrower, older accessor `router.ts::navigate` actually needs — kept as
// its own name rather than inlining `getResolvedSnapshot()?.app_dir` at every
// call site, the same reason `carries` takes `fromAppDir` and not the whole
// resolution: the carry rule is a fact about ONE path, not about the sha or
// the extracted directory.
export function getSnapshotAppDir(): string | null {
  return resolvedSnapshot ? resolvedSnapshot.app_dir : null;
}

// Whether `toPath` sits inside `fromAppDir` — the same folder, or anywhere
// under it. `fromAppDir` is null whenever nothing has resolved a snapshot's
// app folder yet (including "there is no `_snapshot` right now"), and null
// NEVER carries: a fact that has not resolved is not carried on a guess, the
// same "never invented where absent" posture `_side`'s carry rule takes for
// an unknown provenance (see router.ts's navHintIsDir comment).
export function carries(fromAppDir: string | null, toPath: string): boolean {
  if (!fromAppDir || !toPath) return false;
  if (toPath === fromAppDir) return true;
  const base = fromAppDir.endsWith("/") ? fromAppDir : fromAppDir + "/";
  return toPath.startsWith(base);
}

// The one rewrite rule, mirroring static/runtime.js's `rewritePath` exactly:
// a path at or under the resolved snapshot's app folder maps to the same
// relative path under its extracted tree; anything else — including
// `null`/non-absolute input, or no resolution yet — is left alone. Kept in
// lockstep with the runtime's copy by tests/test_runtime_snapshot.py (the
// runtime side) and platform/lib/snapshot-param.test.ts (this side) asking
// the same questions of both.
export function rewriteSnapshotPath(path: string): string {
  if (!resolvedSnapshot || typeof path !== "string" || path[0] !== "/") return path;
  const { app_dir, dir } = resolvedSnapshot;
  if (path === app_dir) return dir;
  if (path.indexOf(app_dir + "/") === 0) return dir + path.slice(app_dir.length);
  return path;
}
