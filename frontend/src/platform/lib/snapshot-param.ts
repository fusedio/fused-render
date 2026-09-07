// Everything the `_snapshot` shell URL param needs: the carry rule, the
// resolved-snapshot singleton, the rewrite rule, and the small pure helpers
// (`isSha`, `shortSha`, `snapshotSrc`, `snapshotListing`) Preview.tsx and
// Listing.tsx build the feature's actual UI out of.
//
// LIVES IN platform/, NOT apps/explorer/, though the feature is explorer-only
// — the reason is the same one appEntry.ts, appAnnotation.ts and
// dismissOnOutside.ts already state for themselves: platform/ never imports
// from apps/ (no existing platform module does, and router.ts's own comments
// about /apps/<tag>/<name> never resolve to an import) — it is the boundary
// that keeps a platform-level concern like routing from depending on any one
// app's internals. `router.ts::navigate` is exactly such a concern, so the
// carry rule it calls has to sit beside it.
//
// ONE MODULE, NOT TWO: this used to be split with a thin re-export living at
// apps/explorer/lib/snapshot-param.ts, each with its own test file — two
// names for the same rule invited exactly the "which one do I edit" question
// a later change would eventually get wrong (code review finding A2). Every
// function here is pure string/object logic with no apps/ dependency, so
// there was no boundary reason for the split: apps/explorer (Preview.tsx,
// Listing.tsx) imports this module directly, the same direction router.ts
// already takes.
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
  // The snapshot's OWN entry page — `GET /api/git/snapshot`'s `entry` field,
  // computed by `app_entry()` against the EXTRACTED tree, never the live one
  // (fused_render/server/routers/git_snapshot.py). Optional: only the app
  // page's own hook (shell/useAppPageSnapshot.ts) currently has a caller that
  // asks what a COMMIT's entry page was; the explorer's consumers (Preview.tsx,
  // Listing.tsx, useSnapshotForFolder.ts) preview one specific FILE, not "the
  // app's entry", and never read this. Code review finding 3 (app page
  // version dropdown pass): a caller holding only `dir`/`app_dir` and
  // rewriting the LIVE entry's path against them gets the wrong FILENAME the
  // moment the entry was renamed between the commit and HEAD —
  // `rewritePathAgainst` only swaps a directory prefix, and can never recover
  // a changed filename. `null` means the extracted tree genuinely has no
  // entry at that commit (same meaning as `getAppEntry`'s live `entry: null`).
  entry?: string | null;
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
// a path at or under `snap`'s app folder maps to the same relative path
// under its extracted tree; anything else — including `null`/non-absolute
// input, or no resolution at all — is left alone. Kept in lockstep with the
// runtime's copy by tests/test_runtime_snapshot.py (the runtime side) and
// platform/lib/snapshot-param.test.ts (this side) asking the same questions
// of both.
//
// Takes `snap` EXPLICITLY rather than only reading the module singleton —
// code review finding [1], round 2: a caller that already holds its own
// validated resolution (Preview.tsx's `snapshotResolved`, re-derived from
// THIS component's own `resolvedSnapshotState` and re-checked against the
// URL's sha) must rewrite against THAT, not against whatever the singleton
// happens to hold at call time. Two apps in one repo share shas, and every
// mounted `useSnapshotForFolder`/this component writes the SAME singleton —
// a split view with a Listing on one app and a Preview on another, both
// under the same sha, could have the singleton land on either one's
// resolution by the time this runs. `rewriteSnapshotPath` (below) is the
// singleton-reading convenience for callers with no local resolution of
// their own (Listing.tsx's `snapshotListing`); this is the one the rewrite
// rule is actually defined in terms of.
export function rewritePathAgainst(
  snap: ResolvedSnapshot | null,
  path: string
): string {
  if (!snap || typeof path !== "string" || path[0] !== "/") return path;
  const { app_dir, dir } = snap;
  if (path === app_dir) return dir;
  if (path.indexOf(app_dir + "/") === 0) return dir + path.slice(app_dir.length);
  return path;
}

// The singleton-reading convenience — everything that has no local
// resolution of its own to rewrite against calls this instead of threading
// `getResolvedSnapshot()` through by hand.
export function rewriteSnapshotPath(path: string): string {
  return rewritePathAgainst(resolvedSnapshot, path);
}

// A hex object name, full or abbreviated — the same shape `/api/git/snapshot`
// accepts and the runtime re-checks before it builds a read URL. Validated on
// the way IN (the ancestor-window hook, Preview.tsx) so a junk value can
// never become a param.
//
// LIVES HERE, alongside the carry rule and the singleton, rather than only in
// apps/explorer: a single module is one source of truth for everything this
// feature needs on both sides of the platform/apps boundary (code review
// finding A2 — this module and apps/explorer/lib/snapshot-param.ts used to
// duplicate a test file each for the same rule). Every one of the functions
// below is pure string/object logic with no dependency on anything apps/
// specific, so there is no boundary reason to keep them out of platform/;
// apps/explorer imports this module directly, the same direction router.ts
// already takes.
const SHA_RE = /^[0-9a-fA-F]{4,64}$/;

export function isSha(value: unknown): value is string {
  return typeof value === "string" && SHA_RE.test(value);
}

// The pill's short form — seven characters, the same abbreviation the git
// template's rows and `git log --oneline` show, so the listing's banner and
// the sidebar's commit list read as the same commit rather than as two ids.
export function shortSha(sha: string): string {
  return sha.slice(0, 7);
}

// `_snapshot` onto a content frame's src — the mechanism that makes it reach
// templates at all. The runtime reads params off its OWN frame src
// (`ownQuery`, static/runtime.js), so a param that is not forwarded here is
// invisible in there. Null src (the `_listing` sentinel, an unresolved mode)
// stays null — there is no frame.
export function snapshotSrc(src: string | null, sha: string | null): string | null {
  if (src === null || sha === null) return src;
  return src + "&_snapshot=" + encodeURIComponent(sha);
}

// What Listing.tsx needs to decide for one folder, in one pure call: is it
// inside the CURRENTLY resolved snapshot's app folder, and if so what should
// actually be fetched. `listPath` is `fsPath` itself whenever `inSnapshot` is
// false (no active snapshot, or this folder sits outside its app) — the
// ordinary, unrewritten case — so a caller need not branch twice on the same
// fact. Consults the shared singleton rather than taking it as a parameter,
// same as `rewriteSnapshotPath`: this and static/runtime.js's `rewritePath`
// are the two places the one rewrite rule is applied, and both read off
// whatever `/api/git/snapshot` last resolved.
export function snapshotListing(
  fsPath: string
): { inSnapshot: boolean; listPath: string } {
  const snap = getResolvedSnapshot();
  const inSnapshot = snap !== null && carries(snap.app_dir, fsPath);
  return { inSnapshot, listPath: inSnapshot ? rewriteSnapshotPath(fsPath) : fsPath };
}

// THE shared frame-src composer — extracted out of Preview.tsx's own `srcFor`
// (code review, app page version dropdown pass, root cause): the app page
// re-implemented frame-src composition from scratch for its own three tabs
// instead of reusing this, and every lesson `srcFor` had already learned —
// rewrite `path`, append all three snapshot params, refuse to build a frame
// during the resolve window — was lost in the copy (findings 1, 2 and 4).
// One helper, at least four callers (Preview.tsx's two branches, AppFiles.tsx,
// AppPage.tsx's Overview), is what makes the NEXT frame-building caller reach
// for this instead of re-deriving the same three rules a fifth time.
//
// `path` is rewritten against `snap` UNCONDITIONALLY. That is a no-op for a
// path already outside `snap.app_dir` — which covers a caller that already
// resolved its own path to the extracted tree (AppPage.tsx's Overview passes
// `snap.entry` itself, already under `snap.dir`, not `snap.app_dir` — see that
// field's own comment on why a directory-prefix swap could never have
// produced it) exactly as safely as it covers the ordinary case of a still-live
// path a caller hands in unchanged (AppFiles.tsx's `file`, Preview.tsx's
// `fsPath`).
//
// `sha` is the URL's raw `_snapshot` claim (or null — the caller's own
// component state, kept in sync with the URL regardless of resolve outcome,
// mirroring Preview.tsx's own `snapshotSha`); `snap` is THIS caller's own
// resolution of it, never a shared singleton (see `rewritePathAgainst`'s own
// comment on why). Returns `null` exactly when `sha` names something not yet
// (or not successfully) resolved into `snap` — Preview.tsx's `snapshotPending`
// gate (finding 4, and its own `framePending` comment on why a `null` src,
// not a live one, is what closes the mixed-era window on first paint): a
// frame built during this window has nothing honest to address — the live
// document is the wrong era, and there is no extracted one yet to point at.
//
// `extra` is any additional `&key=value` segments a caller's own mode needs
// (`_file`, `_remote`, the preview thumbnail flags) — appended between the
// rewritten path and the three snapshot params; order carries no meaning to
// a query string, so callers need not match `srcFor`'s historical ordering.
export function snapshotFrameSrc(opts: {
  snap: ResolvedSnapshot | null;
  sha: string | null;
  path: string;
  extra?: string;
}): string | null {
  const { snap, sha, path, extra = "" } = opts;
  if (sha !== null && snap === null) return null; // pending — see comment above
  const renderPath = rewritePathAgainst(snap, path);
  const snapParams = snap
    ? `&_snapshot_dir=${encodeURIComponent(snap.dir)}&_snapshot_app=${encodeURIComponent(snap.app_dir)}`
    : "";
  return snapshotSrc(
    `/render?path=${encodeURIComponent(renderPath)}${extra}${snapParams}`,
    sha
  );
}
