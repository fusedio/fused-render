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
// two callers that actually own resolving and setting the app dir.
//
// THE CARRY RULE. `_snapshot=<sha>` survives a navigation exactly when the
// DESTINATION is still inside the app folder the sha was resolved against —
// the same file, a sibling file, a subfolder, all carry; a breadcrumb hop out
// of the app folder, or a hop into an unrelated app, both drop it. A sha from
// one app's history says nothing about another app, or about the workspace
// above it.

// The app folder the CURRENT `_snapshot` sha was resolved against — i.e. the
// live `app_dir` the last `/api/git/snapshot` response named. NOT part of the
// URL (only the sha is): only one sha can be "the" snapshot for this shell
// session at a time, and its app folder is a fact about that resolution, not
// a second piece of state a hand-typed or bookmarked URL has to agree with.
//
// A plain module-level variable, not a param and not sessionStorage: it needs
// no persistence of its own. A reload re-resolves it from the URL's
// `_snapshot` plus the current path exactly as the first load did (whichever
// code resolves `/api/git/snapshot` for this document sets it, both on
// selecting a commit and on a fresh load that already carries the param) —
// which is what makes a copied link work in a browser session that never ran
// this JS before.
let currentSnapshotAppDir: string | null = null;

export function setSnapshotAppDir(appDir: string | null): void {
  currentSnapshotAppDir = appDir;
}

export function getSnapshotAppDir(): string | null {
  return currentSnapshotAppDir;
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
