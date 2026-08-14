// The preview pane's dragged width, for the lifetime of the DOCUMENT: one
// fraction shared by every folder and every file, held in a module variable and
// written to no storage at all.
//
// IT USED TO BE PER FOLDER — a `panew` key in the per-path viewstate map — and
// that was the bug. A width is a statement about this window and this pair of
// panes, not about the folder that happened to be open when the divider moved,
// so remembering it per path meant the divider jumped on ordinary navigation:
// out of a folder you had dragged, into a sibling you had not, and the pane
// snapped between your width and the default every time. The file preview's own
// `_side` sidebar had always used a single width and never had this problem. Now
// the listing pane doesn't either — and as of D280 the two even share the same
// default share, 30%.
//
// MEMORY ONLY, DELIBERATELY — this is not a missing feature:
//   • it survives everything the SHELL does, because the shell navigates by
//     history.pushState (platform/lib/router) and never reloads the document.
//     Folder → folder, folder → file, Back and Forward all keep the width.
//   • a REFRESH clears it, and the pane goes back to the plain 30% default
//     (D280 — it used to go back to following the container's width through
//     `defaultPaneFrac`'s breakpoints, which is the machinery that decision
//     deleted). That reset is still the escape hatch: a dragged width otherwise
//     holds for the whole session, and the way back has to be something a user can
//     find without being told about a gesture. Reloading a page is that.
// sessionStorage would survive the refresh and localStorage the browser, so
// both would take the escape hatch away. Neither is an option here.

// null = NO CHOICE MADE, which is a real state and not a missing number: the
// pane then follows the container (pane.ts). Only a completed drag sets it.
let chosen: number | null = null;

export function getPaneFrac(): number | null {
  return chosen;
}

export function setPaneFrac(frac: number | null): void {
  chosen = frac;
}
