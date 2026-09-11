// Decision 2 (and its later fixup): what a keystroke means to the
// completion dropdown, as a pure function of (key, whether the dropdown is
// showing, the current highlight, how many rows there are) — no DOM, no
// router, no state setters. Listing.tsx's onKeyDown is a thin dispatch over
// this; every side effect (moving the highlight, writing text into the
// field, navigating, falling through to decision 5) lives there.
//
// The one property this function exists to guarantee: ENTER'S MEANING MUST
// NOT DEPEND ON WHETHER THE DROPDOWN HAS RENDERED. The dropdown opens after
// a debounce (INSTANT_DEBOUNCE_MS), so if "is a row selected" were true the
// instant a dropdown appeared, an Enter pressed just after the debounce
// would behave differently from the identical Enter pressed just before
// it — same keystroke, different outcome, purely from typing speed. That
// can only be true if the CALLER seeds `highlight` at -1 until the user
// has actually pressed ArrowDown/ArrowUp (Listing.tsx's reset effect does
// this) — this function's `enter-accept` branch is reached only when
// `highlight >= 0`, and never manufactures a selection on its own.
export type CompletionKeyAction =
  | { type: "move"; delta: 1 | -1 }
  // Tab always takes a row when the dropdown is open: the highlighted one,
  // or the first when nothing has been arrowed to yet (shell-completion
  // convention — Tab works without arrowing first). This is the one place
  // Tab and Enter deliberately differ from each other.
  | { type: "tab-accept"; index: number }
  // An EXPLICITLY highlighted row (the user arrowed to it) — the caller
  // navigates into it.
  | { type: "enter-accept"; index: number }
  // Nothing highlighted (including "no dropdown showing at all") — the
  // caller falls through to decision 5's resolve-and-navigate and then
  // decision 4's search gate, exactly as if there were no dropdown.
  | { type: "enter-passthrough" }
  | { type: "none" };

export function completionKeyAction(
  key: string,
  showCompletion: boolean,
  highlight: number,
  itemCount: number,
  // FINDING 1 (code review, 2026-09-10): what Tab targets with NOTHING
  // explicitly arrowed to yet. Defaults to 0 ("the first row") to keep
  // every existing caller and test exactly as it was; a caller that folds
  // a non-completion row (an action row) into row 0 of this same index
  // space passes the index of the first REAL completion instead, so an
  // un-arrowed Tab always completes text rather than running that row —
  // this function still knows nothing about what a row IS, only which
  // index counts as "nothing arrowed to yet" defaults to.
  tabDefaultIndex = 0,
): CompletionKeyAction {
  if (!showCompletion || itemCount === 0) {
    return key === "Enter" ? { type: "enter-passthrough" } : { type: "none" };
  }
  if (key === "ArrowDown") return { type: "move", delta: 1 };
  if (key === "ArrowUp") return { type: "move", delta: -1 };
  if (key === "Tab") {
    return { type: "tab-accept", index: highlight >= 0 ? highlight : tabDefaultIndex };
  }
  if (key === "Enter") {
    return highlight >= 0
      ? { type: "enter-accept", index: highlight }
      : { type: "enter-passthrough" };
  }
  return { type: "none" };
}

// ArrowDown/ArrowUp's wraparound cycle through the list — pulled out of
// Listing.tsx alongside `completionKeyAction` so it's covered by the same
// tests as the key-to-action mapping above.
//
// -1 (nothing highlighted, the dropdown's own resting state per
// `completionKeyAction`'s comment above) is a special case, not just
// another position the plain modulo wrap would carry through correctly:
// Down from -1 should land on the FIRST row, Up on the LAST — the
// conventional combobox behavior for "nothing selected yet". Plain
// `(highlight + delta + itemCount) % itemCount` gives the right answer for
// Down by coincidence (-1 + 1 == 0), but not for Up ((-1 - 1 + itemCount) %
// itemCount lands one row short of the last), so unselected is handled
// explicitly rather than trusted to the general formula.
export function moveHighlight(highlight: number, delta: 1 | -1, itemCount: number): number {
  if (highlight === -1) return delta === 1 ? 0 : itemCount - 1;
  return (highlight + delta + itemCount) % itemCount;
}
