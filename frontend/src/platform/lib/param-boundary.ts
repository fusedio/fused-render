// THE PARAM BOUNDARY, REFERENCE-COUNTED.
//
// `fused.params` inside a framed page climbs `window.parent` until it runs out
// of same-origin ancestors OR meets a window marked `_fusedParamBoundary`, and
// stops BELOW the boundary (static/runtime.js `findTarget`, D46/D72). Marking
// this window is therefore what makes a framed chat read the `session_id` its
// own `src` carries instead of the host page's URL — without it, every frame on
// `/tasks` reads `/tasks`, finds no session, and shows the chat template's HOME
// screen with an empty composer.
//
// WHY A COUNT AND NOT A BOOLEAN. Two surfaces on the Tasks page frame chats at
// once — the Cards wall and the side peek — and each used to set the flag on
// mount and `delete` it on unmount. Whichever unmounted first took the boundary
// away from the other: switch from Cards to List with a peek open, and the
// peek's frame started climbing to `/tasks` again. The flag is a fact about
// whether this window hosts ANY param-owning frame, so it is held while at
// least one does and cleared when the last one goes.
//
// The explorer's tab and panel shells (Tabs.tsx, Panel.tsx) mark their own
// windows directly and are deliberately left alone: those are whole-document
// shells, one per window, with nothing to share the flag with.
import { useEffect } from "react";

let held = 0;

/** Claim the boundary. Returns the release — call it exactly once. */
export function claimParamBoundary(): () => void {
  held += 1;
  window._fusedParamBoundary = true;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    held -= 1;
    if (held <= 0) {
      held = 0;
      delete window._fusedParamBoundary;
    }
  };
}

/**
 * Hold the boundary while `active`.
 *
 * TRI-STATE CALLERS PASS `false` FOR "NOT YET": the native-chat flag is read
 * asynchronously, and a `null` read as a boolean set the flag and deleted it
 * one paint later — a claim about the window that was never true. Only a caller
 * that KNOWS it is framing a param-owning document should pass `true`.
 */
export function useParamBoundary(active: boolean): void {
  useEffect(() => {
    if (!active) return;
    return claimParamBoundary();
  }, [active]);
}

/** Test seam — the count is module state and bun shares one process per run. */
export function resetParamBoundaryForTests(): void {
  held = 0;
  try {
    delete window._fusedParamBoundary;
  } catch {
    /* no window in this runtime */
  }
}
