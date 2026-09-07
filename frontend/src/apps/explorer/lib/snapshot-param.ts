// The `_snapshot` shell URL param, as decisions rather than as JSX — DOM-free
// like preview-side.ts, because the rules below are invariants a test should
// pin and a React component should not be the only statement of.
//
// WHAT THIS IS. Clicking a commit in the `git` sidebar puts the WHOLE SHELL
// into `_snapshot=<sha>`: every frame under it reads the enclosing app folder
// as `git archive`d that commit (`/api/git/snapshot`, resolved by the runtime
// — see runtime.js — and by every template it drives).
//
// A TOP-LEVEL PARAM — the reverse of what this module's predecessor,
// preview-rev.ts (deleted), argued for. That module refused to be a param
// because a `_rev` would leak onto the next file (`navigate` preserves the
// query across a path change) and into a bookmark. The leak is what is
// wanted here: the state must reach the explorer's own listing and every
// template under the app, which in-memory component state structurally
// cannot — a template lives in another frame entirely. The two leaks
// preview-rev.ts refused are answered rather than ignored: `carries` bounds
// the first to the app folder the sha was resolved against, and a bookmark
// of an app at a commit is a coherent thing to have (see the decisions log
// for the fuller argument, and DECISIONS.md D243's successor entry).
//
// THE CARRY RULE and the app-dir state it is checked against live in
// platform/lib/snapshot-param.ts, NOT here, and that split is deliberate: the
// rule is applied by `router.ts::navigate`, a platform/ module, and
// platform/ never imports from apps/ (no existing platform file does — the
// same boundary appEntry.ts and dismissOnOutside.ts already lean on). This
// module is the explorer-facing surface Preview.tsx and Listing.tsx actually
// call: it re-exports the platform module's rule and state, plus `isSha` and
// `shortSha`, which used to be preview-rev.ts's own — restated here rather
// than imported from a module that no longer exists.
import {
  carries,
  getResolvedSnapshot,
  getSnapshotAppDir,
  rewriteSnapshotPath,
  setResolvedSnapshot,
  type ResolvedSnapshot,
} from "@platform/lib/snapshot-param";

export {
  carries,
  getResolvedSnapshot,
  getSnapshotAppDir,
  rewriteSnapshotPath,
  setResolvedSnapshot,
  type ResolvedSnapshot,
};

// A hex object name, full or abbreviated — the same shape `/api/git/snapshot`
// accepts and the runtime re-checks before it builds a read URL. Validated on
// the way IN (the ancestor-window hook, Preview.tsx) so a junk value can
// never become a param.
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
