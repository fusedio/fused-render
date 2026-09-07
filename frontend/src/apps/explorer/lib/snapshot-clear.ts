// THE one way any surface in this shell drops `_snapshot`: clears the shared
// resolved-snapshot singleton (platform/lib/snapshot-param.ts), writes the
// URL, and tells the git sidebar so its own "previewing" banner and
// Checkout/Revert controls do not outlive the pane they describe.
//
// Extracted because THREE call sites used to each hand-roll this clear, and
// only ONE of them (Preview.tsx's own `backToLive`) remembered the sidebar
// hop (code review, round 3, findings 2 and 3):
//
//   * Preview.tsx's `backToLive` — the sidebar's own "back to live" click,
//     routed here via `window._fusedSnapshotSelected`, and the content
//     pane's own banner button. Had the hop.
//   * Preview.tsx's resolve effect, on a confirmed 404 for THIS pane whose
//     sha no other pane holds — cleared the singleton/URL by hand, with no
//     hop. That is the bug findings 2 was filed against: the pane silently
//     drops to live while the sidebar still shows an ARMED, DESTRUCTIVE
//     Checkout for a version nothing on screen shows any more.
//   * Listing.tsx's own "back to live", which goes through
//     `useSnapshotForFolder.ts`'s OWN `backToLive` — a second, independent
//     hand-rolled copy, also with no hop, despite Preview.tsx's own comment
//     CLAIMING this case was covered (finding 3).
//
// One function, every clear path calls it, is what makes a FOURTH clear path
// (a future one) hard to get wrong the same way again — there is nothing
// left to duplicate.
import { replaceSearch } from "@platform/lib/router";
import { getResolvedSnapshot, setResolvedSnapshot } from "@platform/lib/snapshot-param";
import { writeQueryParam } from "./preview-side";

// The DOM idiom this hop already uses elsewhere in this shell
// (`_fusedFsChanged`, `_fusedAskClaude`/`noteAskClaude`): a plain global
// called directly on a same-origin iframe's own `contentWindow`, not a
// `postMessage` — see Preview.tsx's own former comment on why (both ends are
// meant to fail safe).
//
// Failure modes this must survive, none of them this function's problem to
// solve any further than surviving them:
//   * no sidebar mounted at all (`document.querySelector` finds nothing);
//   * a sidebar mounted but showing something other than `git`
//     (`_fusedSnapshotCleared` absent on that template);
//   * an older build of the git template with no such export.
//   * (finding 7) a side frame that is ever cross-origin or sandboxed —
//     `.contentWindow` on such a frame is still truthy, but reading a
//     property off it throws `SecurityError` rather than returning
//     `undefined`. Latent today (every side frame this shell ever mounts is
//     same-origin) but the comment this replaces asserted "both ends fail
//     safe" without actually guarding against it, so a future cross-origin
//     frame would have thrown, uncaught, from inside a caller that had
//     already written the URL — aborting the rest of that caller's own
//     cleanup. Guarded here, once, rather than at every call site.
function notifySidebarSnapshotCleared(): void {
  try {
    const sideFrame = document.querySelector<HTMLIFrameElement>(
      ".preview-side-frame"
    );
    const clear =
      sideFrame?.contentWindow &&
      (sideFrame.contentWindow as unknown as {
        _fusedSnapshotCleared?: () => void;
      })._fusedSnapshotCleared;
    if (typeof clear === "function") clear();
  } catch {
    // Cross-origin/sandboxed frame, or some other access failure: nothing
    // this function can tell the sidebar in that case either.
  }
}

/** Clear `_snapshot` for the whole shell: the singleton, the URL, and the
 *  git sidebar's own idea of what is previewed. Callers still own their OWN
 *  local mirror of the singleton (Preview.tsx's `resolvedSnapshotState`,
 *  `useSnapshotForFolder`'s local state) — this does not know about those,
 *  the same reason `setResolvedSnapshot` alone never re-renders anything
 *  (see snapshot-param.ts's own comment). Call this, then clear the local
 *  mirror the same way every existing caller already does. */
export function clearShellSnapshot(): void {
  setResolvedSnapshot(null);
  const search = writeQueryParam(
    location.search.replace(/^\?/, ""),
    "_snapshot",
    null
  );
  replaceSearch(location.pathname + (search ? "?" + search : ""));
  notifySidebarSnapshotCleared();
}

/** Disarm the sidebar's OPTIMISTIC "previewing" state after a failed
 *  attempt to select a NEW snapshot (round 4, item 3). `preview()` in the
 *  git template's sidebar arms its own banner and DESTRUCTIVE Checkout
 *  button SYNCHRONOUSLY, before the shell's own resolve (`getGitSnapshot`,
 *  in Preview.tsx's `window._fusedSnapshotSelected`) ever confirms
 *  anything — a transient failure there (no app folder encloses this
 *  path, a mount-backed path, git trouble) used to be swallowed silently,
 *  leaving the sidebar armed against a sha the shell never actually
 *  adopted while the pane itself quietly stayed live.
 *
 *  Calls `clearShellSnapshot` — the same hop every other "nothing is
 *  previewed" case already uses, rather than a fifth bespoke one — but
 *  ONLY when there is genuinely nothing else already confirmed for the
 *  shell to describe instead: reads the singleton FRESH, so a DIFFERENT,
 *  already-confirmed snapshot (this pane, or a companion one) survives a
 *  failed attempt to preview some OTHER commit rather than being clobbered
 *  by it. That one case (already confirmed A, a click at B fails) is left
 *  exactly as swallowing it always was — the sidebar may still show B as
 *  "previewing" until another click corrects it — because correcting it
 *  fully would need the sidebar to accept "sync to sha X", a new hop
 *  beyond this function's own "disarm" contract. */
export function disarmSidebarOnFailedSelect(): void {
  if (!getResolvedSnapshot()) clearShellSnapshot();
}
