// THE APP PAGE'S HEADER AND TAB STRIP, FOLDED TO ICONS WHEN THERE IS NO ROOM
// (Akshil, 2026-09-21: "collapse to icons the tabbar and the top right buttons
// if there is less space and collision").
//
// MEASURED, NEVER A BREAKPOINT — the rule the Tasks toolbar and the list rows
// already follow (shell/row-fit.ts, and the reasons stated there). A width is
// not a proxy for a collision: the header carries an app NAME, the strip five
// words, and a number that fits one app clips the next one's.
//
// Two ladders, one hook (`useStripFit` with a need function of its own):
//
//   header   rung 0  the Export/Share and Open buttons lose their words and
//                    keep their icons (the tooltip still says the whole thing)
//   tab bar  rung 0  the five tabs lose their words and keep their icons
//            rung 1  the version picker loses its "Version" eyebrow
//
// WHAT THE NEED IS. `rowNeed` charges a shrinkable text seat a floor and sums
// the rest, but the header's title is a flex COLUMN (name over folder) inside a
// flex row, and summing a column's children would charge the name and the
// folder as if they sat side by side. So the header's need is stated directly:
// the title is worth `TITLE_FLOOR` — the icon, its gap, and about one word of
// name before the ellipsis — and the actions are worth what they measure.
// The tab strip IS a row of fixed seats, so its need is what its two children
// measure plus the gap between them. Both are fixed points: the need at each
// rung is observed while the strip is rendered at that rung
// (`pickLevelFromNeeds`), never reconstructed.
import { useStripFit } from "./row-fit";

/** The labels the ladders hide. One class on every foldable word, so the
 *  stylesheet has one selector (styles/app-page.css). */
export const APP_PAGE_FIT_LABEL = "app-page-fit-lbl";

export const HEAD_DROPS = [".app-page-actions .app-page-fit-lbl"] as const;
export const TABBAR_DROPS = [
  ".app-page-tabs .app-page-fit-lbl",
  ".app-version-picker-eyebrow",
] as const;

/** What the title is always worth: the 50px mark, its 12px gap, and about one
 *  word of name before the ellipsis. Below this the header is not a header. */
export const TITLE_FLOOR = 160;

function gapOf(el: HTMLElement): number {
  return Number.parseFloat(getComputedStyle(el).columnGap) || 0;
}

/** The header's need at its current rung: title floor + actions as measured. */
export function headNeed(head: HTMLElement): number {
  const actions = head.querySelector<HTMLElement>(".app-page-actions");
  const actionsW = actions ? actions.getBoundingClientRect().width : 0;
  return TITLE_FLOOR + (actionsW > 0 ? gapOf(head) + actionsW : 0);
}

/** The tab bar's need at its current rung: the strip, the picker, the gap. */
export function tabbarNeed(bar: HTMLElement): number {
  let need = 0;
  let seats = 0;
  for (const child of Array.from(bar.children) as HTMLElement[]) {
    const w = child.getBoundingClientRect().width;
    if (!(w > 0)) continue;
    need += w;
    seats += 1;
  }
  return need + Math.max(0, seats - 1) * gapOf(bar);
}

/** How many header rungs are folded; the ref goes on `.app-page-head`. */
export function useAppHeadFit(): [number, (el: HTMLElement | null) => void] {
  return useStripFit(HEAD_DROPS, true, headNeed);
}

/** How many tab-bar rungs are folded; the ref goes on `.app-page-tabbar`. */
export function useAppTabbarFit(): [number, (el: HTMLElement | null) => void] {
  return useStripFit(TABBAR_DROPS, true, tabbarNeed);
}
