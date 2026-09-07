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
// call: it re-exports the platform module's rule and state, plus `isSha`,
// which stays a copy of preview-rev.ts's own (same reason that module gives
// for not importing its shape from elsewhere: it is a small, stable
// invariant, cheaper to restate than to couple two otherwise-unrelated
// modules over).
import { isSha as _isSha } from "@apps/explorer/lib/preview-rev";
import {
  carries,
  getSnapshotAppDir,
  setSnapshotAppDir,
} from "@platform/lib/snapshot-param";

export { carries, getSnapshotAppDir, setSnapshotAppDir };

// The same hex-object-name shape `/api/git/snapshot` accepts.
export const isSha = _isSha;
