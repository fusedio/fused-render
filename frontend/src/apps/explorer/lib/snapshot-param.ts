// The `_snapshot` shell URL param, as decisions rather than as JSX — DOM-free
// like preview-side.ts and the module this replaces (preview-rev.ts), because
// the rules below are invariants a test should pin and a React component
// should not be the only statement of.
//
// WHAT THIS IS. Clicking a commit in the `git` sidebar puts the WHOLE SHELL
// into `_snapshot=<sha>`: every frame under it reads the enclosing app folder
// as `git archive`d that commit (`/api/git/snapshot`, resolved by the runtime
// — see runtime.js — and by every template it drives), not just the one
// content pane preview-rev.ts framed.
//
// A TOP-LEVEL PARAM, REVERSING preview-rev.ts'S FOUNDING ARGUMENT. That module
// refused to be a param because a `_rev` would leak onto the next file
// (`navigate` preserves the query across a path change) and into a bookmark.
// The leak is what is wanted here: the state must reach the explorer's own
// listing and every template under the app, which in-memory component state
// structurally cannot — a template lives in another frame entirely. The two
// leaks preview-rev.ts refused are answered rather than ignored: `carries`
// bounds the first to the app folder the sha was resolved against, and a
// bookmark of an app at a commit is a coherent thing to have (see the
// decisions log for the fuller argument).
//
// THE CARRY RULE and the app-dir state it is checked against live in
// platform/lib/snapshot-param.ts, NOT here, and that split is deliberate: the
// rule is applied by `router.ts::navigate`, a platform/ module, and
// platform/ never imports from apps/ (no existing platform file does — the
// same boundary appEntry.ts and dismissOnOutside.ts already lean on). This
// module is the explorer-facing surface Preview.tsx and Listing.tsx actually
// call: it re-exports the platform module's rule and state, plus `isSha` and
// `shortSha`, both copies of preview-rev.ts's own (same reason that module
// gives for not importing its shapes from elsewhere: they are small, stable
// invariants, cheaper to restate than to couple two otherwise-unrelated
// modules over).
import { isSha as _isSha, shortSha as _shortSha } from "@apps/explorer/lib/preview-rev";
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

// The same hex-object-name shape `/api/git/snapshot` accepts.
export const isSha = _isSha;

// The pill's short form — seven characters, the same abbreviation the git
// template's rows and `git log --oneline` show, so the listing's banner and
// the sidebar's commit list read as the same commit rather than as two ids.
export const shortSha = _shortSha;

// `_snapshot` onto a content frame's src — the mechanism that makes it reach
// templates at all. The runtime reads params off its OWN frame src
// (`ownQuery`, static/runtime.js), so a param that is not forwarded here is
// invisible in there; this is the direct successor of preview-rev.ts's
// `revSrc`, same shape, carrying the shell's URL state instead of in-memory
// component state. Null src (the `_listing` sentinel, an unresolved mode)
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
