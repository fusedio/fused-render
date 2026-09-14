// THE LIST ROW'S META, DROPPED RIGHT-TO-LEFT WHEN THERE IS NO ROOM FOR IT.
//
// A task row is a title that shrinks and a right-hand cluster that does not:
// the age, the message count, the folder chip and the Draft chip are all
// `nowrap` and `flex: 0 0 auto`, so once the title has ellipsised to nothing
// the row simply gets wider than the list — and the list grows a horizontal
// scrollbar, which design-principles §0 forbids outright. It was invisible
// until the side peek gave the frame a reason to be narrow (Akshil, 2026-09-13:
// scrollWidth 533 against clientWidth 504, with the bar under the rows).
//
// MEASURED, NEVER A BREAKPOINT — the rule this app already follows for the
// chat's control strip and its composer row (apps/claude/ui/fit.ts, useFitStrip
// .ts, and the reasons stated at length there). A width is not a proxy for a
// collision: the folder chip carries a folder NAME, the age carries words, and
// a number that fits one workspace's rows clips the next one's.
//
// ── WHY THE NEED IS RECONSTRUCTED RATHER THAN RE-MEASURED ───────────────────
//
// The obvious loop — hide something, look again, hide more — oscillates: the
// row fits *because* something is hidden, so the next measurement says there is
// room, so it comes back, so the row overflows again. `useFitStrip` solves this
// by measuring the natural width once and comparing against a constant; this
// does the same thing from the other end. Each droppable item's own width is
// remembered the last time it was on screen, so the NATURAL need is always
// recoverable from what is rendered now:
//
//     natural = (what the rows need at this level) + (what is hidden costs)
//
// The verdict is then a pure function of two numbers and a list of costs, with
// no feedback into the thing it measures.
//
// The pure half is here so it can be proved without a browser (row-fit.test.ts);
// the hook underneath owns the ResizeObserver and the cache.
import { useLayoutEffect, useMemo, useRef, useState } from "react";

/**
 * THE ORDER THE WIDTH IS SPENT IN, right to left across the row's meta cluster
 * (design.md, Round 3). The age goes first — it is the one fact the row's own
 * tooltip repeats in full — then the message count, then the folder chip, and
 * the Draft chip last, because it is the rarest and the only one of the four
 * that says something is UNSENT. The title ellipsises throughout and is never
 * dropped: it is the row.
 */
export const ROW_DROPS = ["age", "count", "project", "draft"] as const;
export type RowDrop = (typeof ROW_DROPS)[number];

/** Which element each drop is, by class. One place, read by the hook's
 *  measurement and by the stylesheet's rules (styles/tasks.css). */
export const ROW_DROP_SELECTOR: Record<RowDrop, string> = {
  age: ".tasks-row-time",
  count: ".tasks-row-msgs",
  // The folder chip and the Draft chip share one wrapper class
  // (`.schedule-tv-id-shield` — the band that keeps a small pill's hover wash
  // off the row's stretched link), so they are told apart by the pill INSIDE
  // it. `:has()` rather than a new class on either: the wrapper is one
  // component's, used by both, and giving it a second name per caller would be
  // a distinction that exists only for this file.
  project: ".schedule-tv-id-shield:has(.schedule-tv-id)",
  draft: ".schedule-tv-id-shield:has(.tasks-draft-pill), .tasks-row .tasks-draft-pill",
};

/**
 * NO TOLERATED OVERFLOW — and this is where this ladder parts company with
 * `useFitStrip`'s, which spends a few pixels on the fold so a strip sitting a
 * fraction inside its own boundary does not flip on every jitter of a drag.
 *
 * That band is affordable there because the row it guards CLIPS: a couple of
 * pixels over is a couple of pixels nobody sees. Here the toolbar deliberately
 * does not clip (a hidden control is the defect this whole ladder exists to
 * prevent), so tolerated overflow is overflow the reader can see — measured at
 * 2px past the edge, with the New task button's border poking out of the row.
 *
 * Kept as a named constant at zero rather than deleted: what stops this
 * oscillating is the fixed point (`naturalNeed` reconstructs the same need from
 * any level, so the verdict is a function of the width alone), not a band, and
 * the next reader deserves to be told that in the place they would come looking
 * for the band.
 */
export const FIT_HYSTERESIS = 0;

/**
 * The width a shrinkable text seat is charged however long its words are.
 *
 * A task title ellipsises and has `min-width: 0`, so on its own it can never
 * make a row overflow — which means it must not be what decides that one has.
 * What it IS worth is a floor: below about this much a title is one word and an
 * ellipsis, and only THEN are the marks at the row's other end worth giving up.
 *
 * DELIBERATELY LOW — the spec's order of loss is age → count → project → draft,
 * with "title ellipsises last" (design.md, Round 3), and the floor is what
 * decides how much title has to go before a mark does. At 140 a row whose title
 * had room was charged 140 whatever it was showing, and three marks came off a
 * list that still had 47px of slack in its tightest row (measured at frame 558,
 * 2026-09-13). 72 is about one word and an ellipsis: enough to tell two rows
 * apart, little enough that the marks outlive it.
 */
export const FIT_TEXT_FLOOR = 72;

/**
 * How many of `ROW_DROPS` to hide: 0 keeps every mark, 4 keeps none.
 *
 * `costs[i]` is what dropping `ROW_DROPS[i]` gives back — its own width plus
 * the gap it was charged. Missing or zero costs are skipped rather than
 * treated as progress, so a row that has no Draft chip cannot spend a level on
 * it and arrive at "still too wide, and nothing left to drop".
 */
export function pickRowLevel(
  available: number,
  natural: number,
  costs: readonly number[],
): number {
  if (!(available > 0)) return 0;
  let need = natural;
  let lastUseful = -1;
  for (let level = 0; level < costs.length; level += 1) {
    if (need <= available + FIT_HYSTERESIS) return level;
    const cost = costs[level] ?? 0;
    if (cost > 0) lastUseful = level;
    need -= cost;
  }
  // Out of room and out of marks. The answer is the level that hides everything
  // there is to hide — NOT `costs.length`, because a list with no Draft chips
  // anywhere would otherwise sit one level past the last thing it can spend,
  // which is a state that says "and the draft is folded too" about nothing.
  return lastUseful + 1;
}

/**
 * THE SMALLEST LEVEL WHOSE MEASURED NEED ACTUALLY FITS.
 *
 * `needAt[L]` is `rowNeed` as it was OBSERVED while the row was rendered at
 * level L — not a reconstruction. That is the whole difference, and it is what
 * makes this a fixed point: the answer for a level no longer depends on which
 * level the row happens to be in when the question is asked.
 *
 * ── WHY THE RECONSTRUCTION HAD TO GO ────────────────────────────────────────
 *
 * `naturalNeed` rebuilt level 0's need by adding back a per-rung COST, and each
 * cost was measured as the width of the thing the rung hides. For the list rows
 * that is honest — a folded chip is a chip that is gone. For the TOOLBAR it is
 * not: folding rung 0 hides four label spans (147px measured) *and* takes 8px
 * of horizontal padding off each of the four view buttons, so the seat actually
 * gives back 219px. The reconstruction was 72px short at every folded level,
 * which meant `pickRowLevel` said "level 1" while the row was at 0 and "level
 * 0" while the row was at 1 — a two-cycle. It settled on whichever the last
 * observer callback produced, and in the 736-776px band it settled on 0, with
 * the New task button hanging 26px past the toolbar's right edge and under the
 * peek (measured live, 2026-09-14, frame 736).
 *
 * `FIT_HYSTERESIS` is 0 and staying 0: a band would only have hidden the flap,
 * and the flap was a wrong number rather than a jitter.
 *
 * ── CONVERGENCE ─────────────────────────────────────────────────────────────
 *
 * A level whose need has never been observed is UNKNOWN, and the answer for an
 * unknown one is "go one rung deeper than the deepest level we have measured" —
 * which renders it, which measures it, which is how the walk terminates. Levels
 * only ever step by one, the sequence is bounded by the ladder's length, and a
 * level that fits is returned immediately on the next read.
 */
export function pickLevelFromNeeds(
  available: number,
  needAt: readonly (number | undefined)[],
  rungs: number,
): number {
  if (!(available > 0)) return 0;
  let deepest = -1;
  for (let level = 0; level <= rungs; level += 1) {
    const need = needAt[level];
    // AN UNKNOWN LEVEL IS A LEVEL WORTH TRYING, and this is the whole of the
    // walk. Returning it renders it, which measures it, which answers the
    // question for good. Because the scan runs from 0 UPWARD, the one returned
    // is always the shallowest thing still worth trying — the least-folded row
    // that might fit — so the walk moves towards showing more, never less.
    //
    // Stopping at the first hole instead was a trap (Bugbot, PR #1138): a cache
    // dropped while the row was folded left nothing known below the current
    // level, the scan broke at level 0 and answered "one past the deepest known"
    // — which was the level it was already on. The toolbar's labels, and the
    // peek header's project and Open door, then stayed folded for ever however
    // much room came back.
    if (need === undefined) return level;
    if (need <= available + FIT_HYSTERESIS) return level;
    deepest = level;
  }
  // Every level is known and none of them fits: the last rung is all there is.
  return Math.min(deepest + 1, rungs);
}

/** What the rows need at level 0, rebuilt from what is on screen plus what this
 *  level has already taken away. Pure, so the reconstruction is testable.
 *
 *  STILL USED BY `useRowFit` (the list's own meta ladder), where every rung
 *  hides a whole element and the cost really is that element's width. The
 *  TOOLBAR and the peek header use `pickLevelFromNeeds` instead — see its note
 *  for the geometry that broke this one. */
export function naturalNeed(
  measured: number,
  level: number,
  costs: readonly number[],
): number {
  let need = measured;
  for (let i = 0; i < level && i < costs.length; i += 1) need += costs[i] ?? 0;
  return need;
}


// ---- measuring a row's NATURAL need ------------------------------------------

/**
 * WHAT A FLEX ROW WOULD NEED ON ONE LINE — and deliberately NOT `scrollWidth`.
 *
 * `scrollWidth` is floored at `clientWidth`: a row that FITS reports the box's
 * width rather than its content's, so a folded toolbar asked "do the words fit
 * now?" was told "you need the whole box" and stayed folded for ever (the exact
 * "stays collapsed even with room" failure `useFitStrip` documents). The need
 * has to be summed from the seats, the way `apps/claude/ui/fit.ts` sums its own.
 *
 * Two seats are charged more than they currently occupy, because both are
 * SQUEEZABLE and their squeezed size is not what they want:
 *
 *   * text that ellipsises (`.tasks-title`) — `scrollWidth` on an
 *     `overflow: hidden` box is the untruncated content, which is exactly the
 *     number wanted;
 *   * a seat with slack of its own (the toolbar's search box) — it publishes
 *     the width it must KEEP as `--fit-natural`, and is charged that. Not what
 *     it opens at: everything above the floor is slack, and slack is spent
 *     before any word in the row folds. Read from CSS rather than cached from a
 *     wide moment: a page first painted narrow has never seen the wide one.
 */
/** Does this child lay its OWN children out as seats — i.e. is it a box whose
 *  want is the sum of theirs, rather than a run of text that ellipsises? */
function isBox(style: CSSStyleDeclaration): boolean {
  const display = style.display;
  return (
    display === "flex" ||
    display === "inline-flex" ||
    display === "grid" ||
    display === "inline-grid"
  );
}

export function rowNeed(scope: HTMLElement, restore?: readonly number[]): number {
  const cs = getComputedStyle(scope);
  const gap = Number.parseFloat(cs.columnGap) || 0;
  let need = (Number.parseFloat(cs.paddingLeft) || 0) + (Number.parseFloat(cs.paddingRight) || 0);
  let seats = 0;
  for (const child of Array.from(scope.children) as HTMLElement[]) {
    const style = getComputedStyle(child);
    // OUT OF FLOW COSTS NOTHING. The row's navigation is an empty `<a>`
    // stretched over the whole row (`.tasks-rowlink`, position: absolute), so
    // its rect is the row's own width — charged as a seat it made every row
    // read as twice too wide and pinned the ladder at its last rung.
    if (style.position === "absolute" || style.position === "fixed") continue;
    const rect = child.getBoundingClientRect();
    if (style.display === "none") {
      // FOLDED BY THE LADDER ITSELF — put it back, at the width it had when it
      // was last on screen, so THIS row's natural need is this row's own.
      //
      // Adding the cached costs to the WIDEST row's measurement instead (which
      // is what `naturalNeed` does, and what this used to do) double-counts
      // across rows: the widest row is rarely the one carrying the widest
      // folder chip, so the sum was a row that does not exist and the ladder
      // folded marks off every row to pay for it (measured live at frame 558:
      // three marks gone with 226px of the row standing empty).
      if (!restore) continue;
      const at = ROW_DROPS.findIndex((drop) => child.matches(ROW_DROP_SELECTOR[drop]));
      const back = at >= 0 ? (restore[at] ?? 0) : 0;
      // The cached cost carries the gap it was charged; the gap is added once
      // per seat below, so only the width itself goes in here.
      if (back - gap <= 0) continue;
      seats += 1;
      need += back - gap;
      continue;
    }
    seats += 1;
    const declared = Number.parseFloat(style.getPropertyValue("--fit-natural")) || 0;
    const grow = Number.parseFloat(style.flexGrow) || 0;
    const shrink = Number.parseFloat(style.flexShrink) || 0;
    // What the seat's own CONTENT is worth. Zero for an empty box — which is
    // exactly what a spacer is.
    const content = child.scrollWidth;
    if (declared > 0) {
      // A seat that publishes the width it must KEEP (`--fit-natural`, the
      // toolbar's search box): charged that, however far flex has squeezed it
      // and however wide it currently sits. Its slack is the row's to spend.
      need += declared;
    } else if (grow > 0 && !child.children.length && !(child.textContent ?? "").trim()) {
      // SLACK IS NOT NEED, and charging it was the whole of the over-folding.
      // `.tasks-grow` is the row's one piece of give made an element: it holds
      // whatever is left over, so its rect was 468px of EMPTY SPACE on a 686px
      // row — and every row therefore read as 468px too wide and folded all
      // four of its marks while two thirds of it stood empty (measured live,
      // 2026-09-13). `fit.ts` states the same rule for the composer row's
      // spacer: a spacer is a seat whose width is slack rather than content.
      //
      // EMPTY AND GROWING, both — and `scrollWidth` cannot be the test, which
      // is what the first fix got wrong: on an empty block it is floored at the
      // element's own width, so the spacer reported 468px of "content". What a
      // flexible empty seat actually needs is its declared floor and nothing
      // more. An empty seat that does NOT grow (the disclosure gutter, drawn
      // whether or not it holds a chevron) keeps its real width below.
      need += Number.parseFloat(style.minWidth) || 0;
    } else if (shrink > 0 && isBox(style) && child.children.length > 0) {
      // A SHRINKABLE CONTAINER — the toolbar's filter group, which is a flex
      // row of its own. Its USED width says nothing about what it wants: flex
      // had squeezed it to 140px around 173px of controls, so the Project chip
      // was drawn straight through the New task button beside it — and the
      // ladder, reading the squeezed box, thought the toolbar still fitted and
      // never folded a thing (measured live at frame 437, 2026-09-13).
      //
      // So a container is charged what ITS OWN seats want, recursively. Same
      // rule as everything else here: measure the content, never the box the
      // content has been crushed into.
      need += rowNeed(child);
    } else if (shrink > 0) {
      // A SEAT THAT IS MEANT TO GIVE WAY — the row's title, which ellipsises by
      // design and floors at nothing. Charging it what its text WANTS
      // (`scrollWidth`) made a long title look like an overflow the meta could
      // fix, and the ladder maxed out on every row with a sentence in it. What
      // it is charged is the floor below which it stops being a title at all;
      // everything past that is the ellipsis doing its job.
      need += Math.min(rect.width, FIT_TEXT_FLOOR);
    } else {
      need += Math.max(rect.width, content);
    }
  }
  if (seats > 1) need += gap * (seats - 1);
  return need;
}

/** How many rows are sampled when the per-item costs are re-read. The widest
 *  row governs, and walking eight hundred of them on a resize frame would cost
 *  more than the scrollbar it is preventing — the first screenful is where the
 *  long folder names and the Draft chips are, and a cost only ever grows. */
const SAMPLE_ROWS = 30;

function measureCosts(scroller: HTMLElement, into: number[]): void {
  const rows = scroller.querySelectorAll<HTMLElement>(".tasks-row");
  const gap = Number.parseFloat(getComputedStyle(scroller).getPropertyValue("--tasks-row-gap")) || 0;
  const limit = Math.min(rows.length, SAMPLE_ROWS);
  for (let i = 0; i < limit; i += 1) {
    const row = rows[i];
    if (!row) continue;
    ROW_DROPS.forEach((drop, at) => {
      const el = row.querySelector<HTMLElement>(ROW_DROP_SELECTOR[drop]);
      if (!el) return;
      const width = el.getBoundingClientRect().width;
      // Zero means "not laid out" — hidden by this very level — and says
      // nothing about what it costs. Only a real width updates the cache.
      if (width <= 0) return;
      const cost = width + gap;
      if (cost > (into[at] ?? 0)) into[at] = cost;
    });
  }
}

/**
 * Watch a task list and answer how many of its meta marks have to go.
 *
 * The returned number is written to the scroller as `data-fit` by the caller;
 * the stylesheet does the hiding, cumulatively (styles/tasks.css).
 */
export function useRowFit(ref: React.RefObject<HTMLElement>, enabled = true): number {
  const [level, setLevel] = useState(0);
  // The cache outlives every measurement: an item's width is a fact about the
  // CONTENT, and re-reading it while the item is hidden would read zero.
  const costs = useRef<number[]>([]);
  const levelRef = useRef(0);
  levelRef.current = level;
  useLayoutEffect(() => {
    // OFF MEANS NOTHING IS OBSERVED. The ladder arrived with the side peek and
    // it is part of what the flag turns off, so with the feature down there is
    // no ResizeObserver, no MutationObserver and no `data-fit` — the list is
    // the list this page has always rendered (shell/task-peek-flag.ts).
    if (!enabled) return;
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const read = () => {
      measureCosts(el, costs.current);
      // The WIDEST row governs — one row over the edge is one scrollbar — and
      // each row's need is reconstructed from ITS OWN folded marks (see
      // `rowNeed`'s `restore`), never from a sum of maxima taken across rows.
      const rows = el.querySelectorAll<HTMLElement>(".tasks-row");
      let need = 0;
      for (let i = 0; i < Math.min(rows.length, SAMPLE_ROWS); i += 1) {
        const row = rows[i];
        if (row) need = Math.max(need, rowNeed(row, costs.current));
      }
      const next = pickRowLevel(el.clientWidth, need, costs.current);
      setLevel((cur) => (cur === next ? cur : next));
    };
    // READ IN THE CALLBACK, not on a frame. A `requestAnimationFrame` hop is
    // the tidier shape and it is the wrong one here: a ResizeObserver already
    // delivers after layout and before paint, and there are real hosts where
    // rAF never fires at all (a pane the compositor has parked — the shell's
    // own preview pane is one), which turned the whole ladder off and left the
    // scrollbar it exists to prevent. Convergence comes from the verdict being
    // a fixed point (`pickRowLevel`) and from writing nothing when it has not
    // changed, not from the delay.
    const schedule = () => {
      if (frame) return;
      frame = 1;
      try {
        read();
      } finally {
        frame = 0;
      }
    };
    read();
    const ro = new ResizeObserver(schedule);
    ro.observe(el);
    // The CONTENT changes width with the box standing still — a poll lands new
    // rows, a folder chip appears once the list spans projects. Children only,
    // and never the scroller's own attributes: this hook writes `data-fit`
    // there, and observing what it writes would schedule the next verdict for
    // ever (useFitStrip's own note).
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [ref, enabled]);
  return enabled ? level : 0;
}

// ---- the Tasks toolbar -------------------------------------------------------

/**
 * THE TOOLBAR'S WORDS, dropped in the order they can be spared (design.md,
 * Round 3). The view toggle goes icon-only first — four shapes a reader
 * recognises before they read anything, which is why the icons were added in
 * the first place — then the two filter triggers, and "New task" last, because
 * it is the one control on the row that STARTS something.
 *
 * Each entry names every label that goes at that level; the cost is their sum,
 * since they are hidden together.
 */
export const TOOLBAR_DROPS = [
  // ---- words → marks. Nothing leaves the row; it stops being spelled out.
  ".schedule-view-seg .schedule-fit-lbl",
  ".schedule-tv-filter-btn .schedule-fit-lbl",
  ".schedule-new .schedule-fit-lbl",
  // ---- and then CONTROLS LEAVE, lowest priority first (design.md, Widths v2:
  // Project filter, Status filter, search, Calendar, Cards, Board; List and
  // New task never go).
  //
  // PROJECT GOES BY MERGING rather than by vanishing, and that is the rung's
  // whole point: the two filter triggers become ONE (TaskFilterControls
  // `merged`), so the Project rows are still a press away under their own
  // heading instead of being unreachable. It is the overflow menu this toolbar
  // would otherwise have to grow. What the rung MEASURES is the Project
  // trigger — the width that stops existing when they merge — and `naturalNeed`
  // adds it back to answer "would they still fit apart?".
  '.schedule-tv-filters .schedule-tv-pop-wrap[data-filter="project"]',
  // …and then the one remaining trigger goes too. Measured as whichever of the
  // two shapes is on screen: the Status trigger while they are still apart (the
  // merged one does not exist yet to be measured), the merged one after.
  '.schedule-tv-filters .schedule-tv-pop-wrap[data-filter="status"], ' +
    '.schedule-tv-filters .schedule-tv-pop-wrap[data-filter="all"]',
  // The search box. It has already given up its slack by now (260 → 72, see
  // `--fit-natural` in styles/schedule.css); this is the 72 going as well.
  ".schedule-tv-search",
  // The three views that are not List, newest-to-oldest in how much the page
  // leans on them. LIST NEVER GOES: it is the default and the one view the page
  // must always be able to get back to.
  '.schedule-view-btn[data-view="calendar"]',
  '.schedule-view-btn[data-view="cards"]',
  '.schedule-view-btn[data-view="board"]',
] as const;

/** The last rung at which a control is merely FOLDED rather than gone. Read by
 *  the page to decide `merged`, and by the stylesheet's comment. */
export const TOOLBAR_MERGE_LEVEL = 4;

/**
 * How many of `TOOLBAR_DROPS` to fold.
 *
 * The toolbar is one row and does not wrap, so its own `scrollWidth` is the
 * honest need — but only because the stylesheet stops its children shrinking
 * (styles/schedule.css). Before that, a narrow bar squeezed the view toggle
 * instead of overflowing it, and the reader got "List | Board" and an empty
 * stub where Cards and Calendar had been clipped away: a control that is
 * PRESENT and unreadable, which is worse than one that is honestly an icon.
 */
export interface ToolbarFit {
  /** How many of `TOOLBAR_DROPS` are folded. */
  level: number;
}

/**
 * THE TOOLBAR NEVER WRAPS ANY MORE (design.md, Widths v2).
 *
 * It used to, as the ladder's last resort: every rung spent and the row still
 * too wide, so it took a second line rather than clipping. That is gone,
 * because the ladder now runs all the way down to a row holding nothing but
 * List and "+" — about 70px — and there is no width at which a second line is
 * the better answer. The toolbar is also the one part of the page the middle
 * pane's floor does not apply to: it stays on ONE LINE at every width, which is
 * exactly what a second line would break.
 */

/**
 * THE PEEK HEADER'S OWN LADDER (design.md, Header + list state v2).
 *
 * The header is one line at every width the panel can be dragged to, down to
 * 220px, and the TITLE is what gives — it ellipsises, which is what a title is
 * for. Everything else in the row is a mark or a control, and a mark that
 * ellipsises is a mark that lies. So when even a floored title will not fit,
 * two things leave, in this order:
 *
 *   1. the project NAME — a fact, repeated on the row behind the panel, and the
 *      door beside it already says where the folder is;
 *   2. the Open door — an act, so it does not simply vanish: it reappears in
 *      the ⋮ (TaskPeek `menuItems`). A hidden control has to be somewhere.
 *
 * The panel-hide button, the chevrons, the status ring, the number and the ⋮
 * are not in this list at any level: hiding the way out of a panel, or the way
 * to its actions, is the one thing a narrow window must not do.
 */
export const PEEK_HEAD_DROPS = [
  ".task-side-peek-project",
  ".task-side-peek-open",
] as const;

/**
 * WATCH ONE ROW AND ANSWER HOW MANY OF ITS SEATS HAVE TO FOLD — the engine
 * under both `useToolbarFit` and the peek header's fit, parameterised only by
 * the ladder.
 *
 * One implementation rather than two, because everything that is subtle here is
 * in the measurement, not in the list: `rowNeed`'s refusal to charge slack,
 * `naturalNeed`'s reconstruction of a need from a folded row, and the fixed
 * point that keeps `pickRowLevel` from oscillating. A second copy is a second
 * place for those to drift.
 */
export function useStripFit(
  drops: readonly string[],
  enabled = true,
): [number, (el: HTMLElement | null) => void] {
  const [level, setLevel] = useState(0);
  // A CALLBACK REF, not a `useRef`, and the difference is the whole reason the
  // first version did nothing: the row is rendered only once the page's state
  // has arrived, so a layout effect keyed on a stable ref object ran ONCE
  // against `null` and never again — the observer was never attached, and the
  // row clipped exactly as before. State makes the node a dependency.
  const [el, setEl] = useState<HTMLElement | null>(null);
  /** What the row NEEDED at each level it has actually been rendered at. The
   *  cache is the measurement (`pickLevelFromNeeds` says why it is not a set of
   *  per-rung costs). */
  const needAt = useRef<(number | undefined)[]>([]);
  const levelRef = useRef(0);
  levelRef.current = level;
  useLayoutEffect(() => {
    // Off, the row is measured by nothing and folds nothing — see `useRowFit`'s
    // note on what the flag's "off" has to mean.
    if (!enabled || !el) return;
    let frame = 0;
    const read = () => {
      const at = levelRef.current;
      const need = rowNeed(el);
      const known = needAt.current[at];
      // CONTENT CHANGED UNDER US — a poll landed a longer folder name, a filter
      // count appeared, the panel swapped to another task — so what we remember
      // about the OTHER levels was taken of a different row.
      //
      // SHIFTED, NOT DROPPED. Whatever the change was, it costs about the same
      // at every level: a folder chip three characters wider is three
      // characters wider whether the view labels are folded or not. So the
      // delta observed here is applied to every level we know, which keeps them
      // all answerable and lets the row unfold in the very next frame. Dropping
      // them instead left a hole under the current level, and a hole is a frame
      // of the row snapping fully open before it folds back — for a poll that
      // lands every twenty seconds.
      //
      // Each entry is corrected for real the next time its own level is
      // rendered, so an estimate that drifts is an estimate with a short life.
      if (known !== undefined && Math.abs(known - need) > 1) {
        const delta = need - known;
        needAt.current = needAt.current.map((v) =>
          v === undefined ? undefined : Math.max(0, v + delta),
        );
      }
      needAt.current[at] = need;
      const next = pickLevelFromNeeds(el.clientWidth, needAt.current, drops.length);
      setLevel((cur) => (cur === next ? cur : next));
    };
    const schedule = () => {
      if (frame) return;
      frame = 1;
      try {
        read();
      } finally {
        frame = 0;
      }
    };
    read();
    const ro = new ResizeObserver(schedule);
    ro.observe(el);
    const mo = new MutationObserver(schedule);
    mo.observe(el, { childList: true, subtree: true, characterData: true });
    return () => {
      ro.disconnect();
      mo.disconnect();
    };
  }, [el, enabled, drops]);
  return [enabled ? level : 0, setEl];
}

export function useToolbarFit(enabled = true): [ToolbarFit, (el: HTMLElement | null) => void] {
  const [level, setEl] = useStripFit(TOOLBAR_DROPS, enabled);
  // An object, because the caller reads `.level` and a bare number would make
  // every call site of this hook change when the shape next grows.
  const fit = useMemo<ToolbarFit>(() => (enabled ? { level } : OFF_FIT), [enabled, level]);
  return [fit, setEl];
}

/** The verdict a disabled toolbar reports: nothing folded. A module constant so
 *  the identity is stable across renders. */
const OFF_FIT: ToolbarFit = { level: 0 };
