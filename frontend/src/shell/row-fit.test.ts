// The fit ladder's arithmetic — what the Tasks list drops from its rows, and
// what the toolbar folds, when the width runs out (shell/row-fit.ts).
//
// Executed rather than grepped, because the failure this prevents is a LOOP:
// hide something, the row fits, so put it back, so the row overflows. The
// reconstruction below is what breaks that cycle, and it is arithmetic.
import { describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const SCHEDULE_CSS = readFileSync(
  join(new URL(".", import.meta.url).pathname, "../styles/schedule.css"),
  "utf8",
);
import {
  FIT_HYSTERESIS,
  FIT_TEXT_FLOOR,
  ROW_DROPS,
  TOOLBAR_DROPS,
  pickLevelFromNeeds,
  TOOLBAR_MERGE_LEVEL,
  naturalNeed,
  pickRowLevel,
  rowNeed,
} from "./row-fit";

// age, count, project chip, Draft chip — plausible measured costs.
/** Every level at or past `from`, as the `:is()` list the stylesheet spells
 *  out — the ladder runs to 9 rungs now (design.md, Widths v2), and writing
 *  them out is what keeps a level from being named once and then overtaken. */
const levelsFrom = (from: number) =>
  Array.from({ length: TOOLBAR_DROPS.length + 1 - from }, (_, i) => `[data-fit="${from + i}"]`);

/**
 * The stylesheet with its comments gone, every run of whitespace collapsed
 * and the padding inside `(…)` and around commas removed — so a selector
 * prettier has broken over eight lines and one it has left on a single line
 * read as the same string here.
 *
 * The alternative is pinning prettier's line-breaking decisions, which is a
 * test that fails on a rename three rungs away.
 */
const FLAT = SCHEDULE_CSS.replace(/\/\*[\s\S]*?\*\//g, " ")
  .replace(/\s+/g, " ")
  .replace(/\(\s+/g, "(")
  .replace(/\s+\)/g, ")")
  .replace(/\s*,\s*/g, ",");

const COSTS = [60, 40, 90, 70];

describe("pickRowLevel", () => {
  it("keeps every mark when the rows already fit", () => {
    expect(pickRowLevel(600, 500, COSTS)).toBe(0);
  });

  it("drops one mark at a time, in the order the width is spent", () => {
    // 640 wide: over by 40, and the age (60) is enough on its own.
    expect(pickRowLevel(600, 640, COSTS)).toBe(1);
    // Over by 90: the age is not enough, the count takes it to 540.
    expect(pickRowLevel(600, 690, COSTS)).toBe(2);
    // Over by 190: age + count leave 630, the folder chip lands it at 540.
    expect(pickRowLevel(600, 790, COSTS)).toBe(3);
    expect(pickRowLevel(600, 900, COSTS)).toBe(4);
  });

  it("stops at the last drop rather than promising a fit it cannot deliver", () => {
    // Nothing left to give: the title ellipsises, which is the row's own job.
    expect(pickRowLevel(200, 2000, COSTS)).toBe(COSTS.length);
  });

  it("tolerates NO overflow — the toolbar it guards does not clip", () => {
    // Exactly at the edge is a fit; one pixel past it is not. The band
    // `useFitStrip` spends on the fold is affordable where the row clips; here
    // a tolerated pixel is a control poking out of the row (measured: the New
    // task button 2px past the edge). What stops this oscillating is the fixed
    // point, not a band — see FIT_HYSTERESIS.
    expect(FIT_HYSTERESIS).toBe(0);
    expect(pickRowLevel(600, 600, COSTS)).toBe(0);
    expect(pickRowLevel(600, 601, COSTS)).toBe(1);
  });

  it("spends no level on a mark that is not there", () => {
    // A list with no Draft chips anywhere: its cost is 0, so dropping it buys
    // nothing and the ladder walks straight past it.
    const noDraft = [60, 40, 90, 0];
    expect(pickRowLevel(600, 800, noDraft)).toBe(3);
  });

  it("answers 0 for a box that has not been laid out yet", () => {
    expect(pickRowLevel(0, 900, COSTS)).toBe(0);
  });
});

describe("naturalNeed", () => {
  it("is the measurement itself when nothing is hidden", () => {
    expect(naturalNeed(500, 0, COSTS)).toBe(500);
  });

  it("adds back exactly what this level took away", () => {
    // THE WHOLE ANTI-OSCILLATION TRICK: what is measured at level 2 is a row
    // that is narrow BECAUSE two marks are gone, and asking "does it fit now"
    // of that number is what puts them back and overflows again.
    expect(naturalNeed(500, 2, COSTS)).toBe(500 + 60 + 40);
    expect(naturalNeed(500, 4, COSTS)).toBe(500 + 60 + 40 + 90 + 70);
  });

  it("cannot add back more than there are drops", () => {
    expect(naturalNeed(500, 9, COSTS)).toBe(500 + 60 + 40 + 90 + 70);
  });

  it("settles: whatever level it is asked from, it lands on the same one", () => {
    // THE PROPERTY THAT MATTERS, and the loop this file exists to break. A
    // list rendered at ANY level measures a different width, but the
    // reconstruction puts the same natural need back — so the verdict is a
    // function of the width alone and the ladder has a fixed point rather than
    // a cycle.
    const natural = 800;
    for (const available of [300, 500, 620, 700, 900]) {
      const settled = pickRowLevel(available, natural, COSTS);
      for (let from = 0; from <= COSTS.length; from += 1) {
        // What the rows would measure if they were rendered at `from`.
        const measured = natural - naturalNeed(0, from, COSTS);
        expect(pickRowLevel(available, naturalNeed(measured, from, COSTS), COSTS)).toBe(settled);
      }
    }
  });
});

describe("ROW_DROPS", () => {
  it("spends the age first and the Draft chip last", () => {
    // The order is a decision (design.md, Round 3), not an implementation
    // detail: the age is repeated in the row's own tooltip, and the Draft chip
    // is the only one of the four that says something is UNSENT.
    expect([...ROW_DROPS]).toEqual(["age", "count", "project", "draft"]);
  });
});

// ---- rowNeed, against a row measured live ------------------------------------
// A hand-built node with the three things `rowNeed` reads off each child: its
// computed style, its rect and its scrollWidth. Not happy-dom: what is under
// test is arithmetic over four numbers per seat, and a real layout engine would
// only make the numbers someone else's.
interface FakeSeat {
  width: number;
  /** `scrollWidth`. NOT 0 for an empty box: on an empty block it is floored at
   *  the element's own width, which is exactly why a spacer cannot be detected
   *  by its content width. The fake models that faithfully. */
  content?: number;
  grow?: number;
  shrink?: number;
  natural?: number;
  position?: string;
  display?: string;
  /** No children and no text — a spacer. */
  empty?: boolean;
  minWidth?: number;
  /** Which ROW_DROP_SELECTOR this seat answers to, for the restore path. */
  drop?: string;
  /** A container: its want is the sum of these, not its own used width. */
  kids?: readonly FakeSeat[];
}

function fakeNode(seat: FakeSeat, gap: number): unknown {
  const kids = (seat.kids ?? []).map((kid) => fakeNode(kid, gap));
  return {
    scrollWidth: seat.content ?? seat.width,
    children: seat.kids ? kids : seat.empty ? [] : [1],
    textContent: seat.empty ? "" : "x",
    matches: (sel: string) => (seat.drop ? sel.includes(seat.drop) : false),
    getBoundingClientRect: () => ({ width: seat.width }),
    getClientRects: () => [1],
    offsetParent: {},
    __style: {
      position: seat.position ?? "static",
      display: seat.display ?? (seat.kids ? "flex" : "block"),
      minWidth: `${seat.minWidth ?? 0}px`,
      flexGrow: String(seat.grow ?? 0),
      flexShrink: String(seat.shrink ?? 0),
      // A container is measured like any row: its own gap, no padding of its
      // own in these fixtures.
      columnGap: `${gap}px`,
      paddingLeft: "0px",
      paddingRight: "0px",
      getPropertyValue: (name: string) =>
        name === "--fit-natural" && seat.natural ? `${seat.natural}px` : "",
    },
  };
}

function fakeRow(seats: readonly FakeSeat[], gap = 8, pad = 12): HTMLElement {
  const row = {
    children: seats.map((seat) => fakeNode(seat, gap)),
    __style: {
      columnGap: `${gap}px`,
      paddingLeft: `${pad}px`,
      paddingRight: `${pad}px`,
      position: "static",
      display: "flex",
      minWidth: "0px",
      flexGrow: "0",
      flexShrink: "0",
      getPropertyValue: () => "",
    },
  };
  (globalThis as { getComputedStyle?: unknown }).getComputedStyle = (
    node: { __style: unknown },
  ) => node.__style;
  return row as unknown as HTMLElement;
}

describe("rowNeed", () => {
  it("does not charge the row for its own empty space", () => {
    // THE ROW AS MEASURED LIVE (2026-09-13, frame 558): 502px wide, a third of
    // it the `.tasks-grow` spacer standing empty at 284px — and every mark
    // folded away regardless. Slack is not need.
    const seats: FakeSeat[] = [
      { width: 35 }, // the disclosure gutter
      { width: 20 }, // the status ring
      { width: 55, content: 55 }, // TASK-nnn
      { width: 31, content: 240, shrink: 1 }, // the title, ellipsising
      // `.tasks-grow` — EMPTY, and its `scrollWidth` reports its own 284px box,
      // which is precisely the trap: it is slack, not content.
      { width: 284, grow: 1, shrink: 1, empty: true, minWidth: 8 },
      { width: 22 }, // the quick door
      { width: 46, content: 46 }, // the age
      { width: 34, content: 34 }, // the message count
      { width: 80, content: 80 }, // the folder chip
    ];
    const need = rowNeed(fakeRow(seats));
    // 35+20+55 + min(31, FLOOR) + 0 + 22+46+34+80 = 323, plus 8 gaps and 24 of
    // padding — comfortably inside the 502 the row actually has.
    expect(need).toBe(35 + 20 + 55 + 31 + 8 + 22 + 46 + 34 + 80 + 8 * 8 + 24);
    expect(need).toBeLessThan(502);
    // …so nothing is folded at all, and certainly not all four marks.
    expect(pickRowLevel(502, need, [46 + 8, 34 + 8, 80 + 8, 0])).toBe(0);
  });

  it("folds at most the age once the row is genuinely tight", () => {
    // The same row with a long folder name and a Draft chip, at the same width.
    const seats: FakeSeat[] = [
      { width: 35 },
      { width: 20 },
      { width: 55, content: 55 },
      { width: 31, content: 900, shrink: 1 },
      { width: 40, grow: 1, shrink: 1, empty: true, minWidth: 8 },
      { width: 22 },
      { width: 46, content: 46 },
      { width: 34, content: 34 },
      { width: 150, content: 150 },
    ];
    const need = rowNeed(fakeRow(seats));
    const costs = [46 + 8, 34 + 8, 150 + 8, 0];
    expect(pickRowLevel(502, need, costs)).toBeLessThanOrEqual(1);
  });

  it("charges an ellipsising title its floor, not the length of its words", () => {
    const long = rowNeed(fakeRow([{ width: 400, content: 4000, shrink: 1 }]));
    expect(long).toBe(Math.min(400, FIT_TEXT_FLOOR) + 24);
    // The floor is low on purpose: the title gives up its width BEFORE any
    // mark does (design.md, Round 3 — "title ellipsises last" is about what
    // survives, not about what shrinks first).
    expect(FIT_TEXT_FLOOR).toBeLessThan(100);
  });

  it("charges a seat its published FLOOR, not the width it happens to have", () => {
    // The toolbar's search box: it opens at 260 and must keep 120. Whether it
    // is currently wide or already squeezed, what it COSTS the row is 120 —
    // everything above that is slack for the row to spend before a word folds.
    expect(rowNeed(fakeRow([{ width: 260, content: 240, natural: 120, shrink: 1 }]))).toBe(
      120 + 24,
    );
    expect(rowNeed(fakeRow([{ width: 120, content: 90, natural: 120, shrink: 1 }]))).toBe(
      120 + 24,
    );
  });

  it("skips a child that is out of flow", () => {
    // The row's navigation is an empty `<a>` stretched over the whole row; its
    // rect is the row's own width, and charging it made every row read as twice
    // too wide.
    const seats: FakeSeat[] = [
      { width: 502, position: "absolute" },
      { width: 55, content: 55 },
    ];
    // One seat, so no gap is charged either.
    expect(rowNeed(fakeRow(seats))).toBe(55 + 24);
  });
});

describe("rowNeed's restore", () => {
  it("puts back only what THIS row has folded, at its cached width", () => {
    // The cross-row bug: the widest row (a long title, no folder chip) was
    // charged the widest folder chip on the page, so the ladder paid for a row
    // that does not exist. Each row's need is now its own.
    const seats: FakeSeat[] = [
      { width: 55, content: 55 },
      { width: 0, display: "none", drop: "tasks-row-time" },
    ];
    const costs = [46 + 8, 0, 0, 0];
    // Two seats, one gap, 24 of padding: 55 + 46 + 8 + 24.
    expect(rowNeed(fakeRow(seats), costs)).toBe(55 + 46 + 8 + 24);
  });

  it("charges nothing for a folded mark this row never had", () => {
    // A row with no Draft chip has no hidden Draft chip to put back; the
    // selector matches nothing, so the seat is simply absent.
    const seats: FakeSeat[] = [{ width: 55, content: 55 }];
    expect(rowNeed(fakeRow(seats), [999, 999, 999, 999])).toBe(55 + 24);
  });

  it("ignores folded marks entirely when no cache is offered", () => {
    const seats: FakeSeat[] = [
      { width: 55, content: 55 },
      { width: 0, display: "none", drop: "tasks-row-time" },
    ];
    expect(rowNeed(fakeRow(seats))).toBe(55 + 24);
  });
});

describe("the toolbar's filter group", () => {
  it("is charged what its CONTROLS want, not the width flex left it", () => {
    // THE NUMBERS MEASURED LIVE at frame 437 (2026-09-13): the group had been
    // squeezed to 140px around a search that had given up all 260 of its width,
    // a 77px Status and an 80px Project — 173px of controls — and the Project
    // chip was being drawn through the New task button beside it.
    const filters: FakeSeat = {
      width: 140, // what flex left it — deliberately NOT what it is charged
      shrink: 1,
      display: "flex",
      kids: [
        { width: 0, content: 0, natural: 0, shrink: 1 }, // the search, fully spent
        { width: 77, content: 77 }, // Status
        { width: 80, content: 80 }, // Project
      ],
    };
    const toolbar = fakeRow([{ width: 125, content: 125 }, filters, { width: 104, content: 104 }]);
    // filters = 0 + 77 + 80 + two 8px gaps = 173, and the row is
    // 125 + 173 + 104 + two 8px gaps + 24 of padding = 442.
    expect(rowNeed(toolbar)).toBe(125 + (77 + 80 + 16) + 104 + 16 + 24);
  });

  it("advances the ladder past level 1 at the width that broke", () => {
    const filters: FakeSeat = {
      width: 140,
      shrink: 1,
      display: "flex",
      kids: [
        { width: 0, content: 0, shrink: 1 },
        { width: 77, content: 77 },
        { width: 80, content: 80 },
      ],
    };
    const need = rowNeed(
      fakeRow([{ width: 125, content: 125 }, filters, { width: 104, content: 104 }]),
    );
    // The toolbar's own costs, as measured: the four view labels, the two
    // filter labels, and "New task".
    const costs = [104, 74, 62];
    // 442 against 393 — over. Level 1 (view labels off) gives back 104 → 338,
    // which fits; but the group's controls alone (173) plus the seg's glyphs
    // still have to fit inside 393, and they do. What must NOT happen is the
    // ladder sitting at 1 while the chips overlap, which is what reading the
    // squeezed box produced.
    const level = pickRowLevel(393, need, costs);
    expect(level).toBeGreaterThanOrEqual(1);
    // …and with a group that is genuinely too wide for the row, it goes further.
    const wider = rowNeed(
      fakeRow([
        { width: 125, content: 125 },
        { ...filters, kids: [...(filters.kids ?? []).slice(0, 1), { width: 130, content: 130 }, { width: 140, content: 140 }] },
        { width: 104, content: 104 },
      ]),
    );
    expect(pickRowLevel(393, wider, costs)).toBeGreaterThanOrEqual(2);
  });

  it("still treats a shrinkable run of TEXT as text, not as a box", () => {
    // The discriminator is `display`, not "does it shrink": a title with a
    // nested mark inside it is still a title.
    const title: FakeSeat = { width: 400, content: 4000, shrink: 1, display: "block" };
    expect(rowNeed(fakeRow([title]))).toBe(FIT_TEXT_FLOOR + 24);
  });
});

describe("the toolbar at its narrowest", () => {
  // THE FRAME THAT BROKE (Akshil, 2026-09-13): viewport 1150, frame 369, so a
  // 325px toolbar. Every seat as measured there, with each one charged what it
  // must KEEP rather than what it currently has.
  const NARROW = 325;
  const seg = { width: 125, content: 125 }; // four icon-only view buttons
  const filters = (triggers: number) => ({
    width: 179,
    shrink: 1,
    display: "flex",
    kids: [
      // The search: opens at 260, must keep 72 (its `--fit-natural`).
      { width: 193, content: 193, natural: 72, shrink: 1 },
      ...Array.from({ length: triggers }, () => ({ width: 76, content: 76 })),
    ],
  });
  const newTask = { width: 104, content: 104 };
  const costs = [104, 74, 62, 76]; // view labels, filter labels, "New task", 2nd trigger

  it("reaches a level that FITS rather than running out and clipping", () => {
    const need = rowNeed(fakeRow([seg, filters(2), newTask], 12, 0));
    const level = pickRowLevel(NARROW, need, costs);
    // Every rung this level spends, spent:
    let left = need;
    for (let i = 0; i < level; i += 1) left -= costs[i] ?? 0;
    expect(level).toBeGreaterThanOrEqual(2);
    expect(left).toBeLessThanOrEqual(NARROW);
  });

  it("measures the merged toolbar at what it actually takes", () => {
    // Level 4 as it renders: icon-only views, ONE filter trigger, a "+" button.
    const merged = rowNeed(
      fakeRow([seg, filters(1), { width: 32, content: 32 }], 12, 0),
    );
    // 125 + (72 + 76 + 12) + 32 + two 12px gaps = 341 — and what the row is
    // charged must never exceed what it has once the ladder has finished.
    expect(merged).toBeLessThanOrEqual(NARROW + costs[3]);
  });

  it("tolerates nothing, because the toolbar does not clip", () => {
    // One pixel over is one pixel of a control outside the row.
    const need = rowNeed(fakeRow([seg, filters(2), newTask], 12, 0));
    let left = need;
    const level = pickRowLevel(NARROW, need, costs);
    for (let i = 0; i < level; i += 1) left -= costs[i] ?? 0;
    expect(left).not.toBeGreaterThan(NARROW);
  });
});

describe("pickLevelFromNeeds — the toolbar's fixed point", () => {
  // THE NUMBERS ARE THE LIVE ONES (2026-09-14, measured on :2652 with the peek
  // open). The toolbar sat at 692px of content with the ladder unfolded and
  // needed 717 — a 25px overflow that put the New task button's right edge 26px
  // past the toolbar and under the peek panel. Folding rung 0 brings it to 498.
  const AVAILABLE_736 = 692; // frame 736 − the page's 44px of gutters
  const NEED_AT_0 = 717;
  const NEED_AT_1 = 498;

  it("folds when the level it is RENDERING does not fit", () => {
    expect(pickLevelFromNeeds(AVAILABLE_736, [NEED_AT_0], 9)).toBe(1);
  });

  it("STAYS folded once the folded level is the one that fits", () => {
    // The two-cycle this replaced: the old reconstruction rebuilt level 0's
    // need by adding back the four label spans (147px measured) — but folding
    // rung 0 also takes 8px of padding off each of the four view buttons, so
    // the seat really gives back 219. 72px short, every time, which made the
    // verdict at level 1 "level 0" and the verdict at level 0 "level 1".
    expect(pickLevelFromNeeds(AVAILABLE_736, [NEED_AT_0, NEED_AT_1], 9)).toBe(1);
    // …and asked again from either side it answers the same thing, which is
    // what "fixed point" means and what the old shape could not do.
    expect(pickLevelFromNeeds(AVAILABLE_736, [NEED_AT_0, NEED_AT_1], 9)).toBe(1);
  });

  it("UNFOLDS the moment level 0's measured need fits again", () => {
    // Frame 776 and up: 732 of content against the same 717.
    expect(pickLevelFromNeeds(732, [NEED_AT_0, NEED_AT_1], 9)).toBe(0);
  });

  it("TRIES an unknown level rather than stopping at it", () => {
    // Bugbot, PR #1138. A cache dropped while the row was folded leaves nothing
    // known below the current level; stopping at the hole answered "the level
    // you are already on" and the row could never unfold again.
    expect(pickLevelFromNeeds(900, [undefined, 600, 500], 9)).toBe(0);
    expect(pickLevelFromNeeds(900, [700, undefined, 500], 9)).toBe(0);
    expect(pickLevelFromNeeds(650, [700, undefined, 500], 9)).toBe(1);
  });

  it("converges to 0 from a fold once the content shrinks", () => {
    // Folded at 2 because the row needed more than it had; then a filter count
    // goes away and every level costs 300 less. The next read must land on 0,
    // not sit at 2 with room to spare.
    const before = [900, 800, 700];
    expect(pickLevelFromNeeds(750, before, 9)).toBe(2);
    const after = before.map((n) => n - 300);
    expect(pickLevelFromNeeds(750, after, 9)).toBe(0);
  });

  it("walks one rung at a time into levels it has never measured", () => {
    // Unknown levels are unknown, and the only way to learn one is to render
    // it. Each read steps once; the walk is bounded by the ladder's length.
    expect(pickLevelFromNeeds(200, [NEED_AT_0], 9)).toBe(1);
    expect(pickLevelFromNeeds(200, [NEED_AT_0, NEED_AT_1], 9)).toBe(2);
    expect(pickLevelFromNeeds(200, [NEED_AT_0, NEED_AT_1, 400], 9)).toBe(3);
  });

  it("stops at the last rung rather than past it", () => {
    const needs = Array.from({ length: 10 }, (_, i) => 900 - i);
    expect(pickLevelFromNeeds(100, needs, 9)).toBe(9);
  });

  it("tolerates nothing — FIT_HYSTERESIS stays 0", () => {
    // A band would have hidden the flap rather than fixed it, and one pixel
    // over is one pixel of a control outside the row.
    expect(FIT_HYSTERESIS).toBe(0);
    expect(pickLevelFromNeeds(717, [NEED_AT_0, NEED_AT_1], 9)).toBe(0);
    expect(pickLevelFromNeeds(716, [NEED_AT_0, NEED_AT_1], 9)).toBe(1);
  });

  it("folds New task to a + before the row can overflow, filter active", () => {
    // A SELECTED filter is the widest the toolbar ever gets: the trigger
    // becomes a split control — glyph, word, count badge, ✕ — so the row asks
    // for ~80px more than it does at rest, exactly when the panel has taken
    // most of the width. The ladder has to reach the rung that folds "+ New
    // task" (3) before anything pokes out of the box.
    //
    // Needs measured with a chip on: 795 unfolded, then the view labels (−219),
    // the filter word (−66), and the New task caption (−72).
    const withChip = [795, 576, 510, 438];
    // A 500px toolbar — about a 544px frame, mid-drag — has to get to 3.
    expect(pickLevelFromNeeds(500, withChip, 9)).toBe(3);
    // …and every level it passes through is one it MEASURED, so no level can
    // report itself as fitting while a control hangs outside the row.
    for (let l = 0; l < withChip.length; l += 1) {
      const need = withChip[l] as number;
      expect(pickLevelFromNeeds(need, withChip, 9)).toBeLessThanOrEqual(l);
      expect(pickLevelFromNeeds(need - 1, withChip, 9)).toBeGreaterThan(l - 1);
    }
  });

  it("keeps the count and the ✕ on a folded filter chip", () => {
    // The badge is the only thing on a folded trigger that says a filter is ON,
    // and the ✕ is the one way to turn it off without opening the menu — so the
    // ladder folds the WORD and the pill's padding, and nothing else.
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(2).join(",")}) .schedule-tv-filter-btn {`,
    );
    for (const kept of [".schedule-tv-filter-count", ".schedule-tv-filter-x"]) {
      expect(SCHEDULE_CSS).not.toContain(`${kept} {\n  display: none;`);
      expect(TOOLBAR_DROPS.some((sel) => sel.includes(kept))).toBe(false);
    }
  });

  it("answers 0 for a row that has not been laid out", () => {
    expect(pickLevelFromNeeds(0, [NEED_AT_0], 9)).toBe(0);
  });
});

describe("the search field's caption", () => {
  it("is whole or it is gone — never clipped mid-word", () => {
    // Measured live at fit 1: the field floors at 72px with 38px of padding, so
    // "Search tasks…" (82px of text) was being cut to "Sea". A container query
    // rather than a ladder rung, because the field's width is decided by flex
    // against whatever the other seats took — no rung names a width.
    expect(SCHEDULE_CSS).toContain("container: tasks-search / inline-size;");
    expect(SCHEDULE_CSS).toContain("@container tasks-search (max-width: 132px)");
    const block = SCHEDULE_CSS.slice(
      SCHEDULE_CSS.indexOf("@container tasks-search (max-width: 132px)"),
    ).slice(0, 200);
    expect(block).toContain(".schedule-tv-search-input::placeholder");
    expect(block).toContain("color: transparent");
    // Scoped to the flag's attribute like every other rule that changes how
    // this toolbar lays out.
    const at = SCHEDULE_CSS.indexOf("container: tasks-search / inline-size;");
    expect(SCHEDULE_CSS.slice(0, at)).toContain(".schedule-toolbar[data-fit] .schedule-tv-search {");
    // …and the FLOOR IS STRICTLY WIDER THAN THE QUERY (design.md, Polish batch
    // 4). `max-width` in a container query is inclusive, so while the two were
    // the same 132 the backstop fired the instant flex rested the field on its
    // own floor — which is the ordinary state of this toolbar at the default
    // panel width, and exactly the "placeholder still not visible" Akshil kept
    // reporting. 140 against 132 leaves the caption 8px of daylight.
    const floor = /--fit-natural: (\d+)px;/.exec(
      SCHEDULE_CSS.slice(SCHEDULE_CSS.indexOf(".schedule-toolbar[data-fit] .schedule-tv-search {")),
    );
    expect(floor?.[1]).toBe("140");
    expect(Number(floor?.[1])).toBeGreaterThan(132);
    expect(SCHEDULE_CSS).toContain("min-width: var(--fit-natural);");
  });
});

describe("the toolbar's ladder, in order", () => {
  // The order is the spec (design.md, Widths v2), and it is the one thing about
  // this ladder a reader would notice being wrong: a Board button that goes
  // before the Project filter is a page that has thrown away the control you
  // were reaching for and kept the one you were not.
  it("spends words first, then controls, lowest priority first", () => {
    expect(TOOLBAR_DROPS.map((sel) => sel.split(" ").pop())).toEqual([
      // words → marks: the view labels, then the filter labels…
      ".schedule-fit-lbl",
      ".schedule-fit-lbl",
      // …then the SEARCH folds to its magnifier, BEFORE "+ New task" loses its
      // words (design.md, Polish batch 4): the widest seat on the row, whose
      // question ⌘K also answers, against the one control here that starts
      // something.
      ".schedule-tv-search",
      ".schedule-fit-lbl",
      // …and then controls leave: Project (by merging), Status, Calendar,
      // Cards, Board.
      '.schedule-tv-pop-wrap[data-filter="project"]',
      '.schedule-tv-pop-wrap[data-filter="all"]',
      '.schedule-view-btn[data-view="calendar"]',
      '.schedule-view-btn[data-view="cards"]',
      '.schedule-view-btn[data-view="board"]',
    ]);
  });

  it("merges the two filters at the rung the page reads", () => {
    // Project stops having a trigger of its own and its rows move under a
    // heading in the merged menu — folded, not taken away. `TOOLBAR_MERGE_LEVEL`
    // is what Scheduled.tsx compares `level` against, so it has to name the
    // rung whose selector is the Project one.
    expect(TOOLBAR_DROPS[TOOLBAR_MERGE_LEVEL - 1]).toContain('[data-filter="project"]');
  });
});

describe("the toolbar's fold rules, in the stylesheet", () => {
  // The ladder is half arithmetic and half CSS, and the CSS half has its own
  // failure: a level named once and then overtaken puts the words back on. The
  // New task label did exactly that — folded at level 3 and BACK at level 4,
  // which is the one level it most needed to be gone (measured live).
  it("folds the New task label at level 4 AND at every level past it", () => {
    // Level 4, not 3: the search takes rung 2 now and pushed this one down
    // (design.md, Polish batch 4).
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(4).join(",")}) .schedule-new .schedule-fit-lbl`,
    );
  });

  it("folds the search to a magnifier at level 3, and never hides it", () => {
    // A 32px square that keeps its place, its tab stop and its press — the
    // field opens over the row on focus rather than widening the seat, which
    // would move the toolbar the moment the reader reached for it.
    const at = FLAT.indexOf(
      `.schedule-toolbar:is(${levelsFrom(3).join(",")}) .schedule-tv-search {`,
    );
    expect(at).toBeGreaterThan(-1);
    const block = FLAT.slice(at).slice(0, 500);
    expect(block).toContain("--fit-natural: 32px");
    expect(block).toContain("flex: 0 0 32px");
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(3).join(",")}) .schedule-tv-search:focus-within .schedule-tv-search-input {`,
    );
    // NO RUNG TAKES IT OFF THE ROW — neither the field nor the group it sits
    // in, which is no longer an empty box at any level.
    expect(FLAT).not.toContain(
      `.schedule-toolbar:is(${levelsFrom(6).join(",")}) .schedule-tv-search {`,
    );
    expect(FLAT).not.toContain(
      `.schedule-toolbar:is(${levelsFrom(6).join(",")}) .schedule-tv-filters {`,
    );
    expect(TOOLBAR_DROPS.filter((sel) => sel === ".schedule-tv-search")).toHaveLength(1);
  });

  it("hides the folded field's VALUE too, and says a filter is on with a dot", () => {
    // Bugbot, 78118e0fa: blanking only the placeholder left a typed query
    // painted into a 32px box with 28px of left padding — two letters wedged
    // against the magnifier and clipped mid-word, which is the "Sea" this whole
    // ladder exists to prevent, with the reader's own words this time.
    const folded = FLAT.indexOf(
      `.schedule-toolbar:is(${levelsFrom(3).join(",")}) .schedule-tv-search .schedule-tv-search-input {`,
    );
    expect(folded).toBeGreaterThan(-1);
    const block = FLAT.slice(folded).slice(0, 400);
    expect(block).toContain("color: transparent");
    // A caret blinking in a box with no text in it is a field claiming a focus
    // it does not have.
    expect(block).toContain("caret-color: transparent");
    // …and it comes BACK when the field opens, restated rather than left to the
    // cascade: the folded rule sets `color` at the same specificity and first.
    const open = FLAT.indexOf(
      `.schedule-toolbar:is(${levelsFrom(3).join(",")}) .schedule-tv-search:focus-within .schedule-tv-search-input {`,
    );
    expect(open).toBeGreaterThan(folded);
    expect(FLAT.slice(open).slice(0, 400)).toContain("color: var(--fg)");
    // A FILTER THAT IS ON SAYS SO — the same rule the filter triggers follow,
    // whose count badge and ✕ survive every rung. `:has()`, because the
    // magnifier is drawn before the input and no sibling combinator reaches it.
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(3).join(",")}) ` +
        ".schedule-tv-search:not(:focus-within)" +
        ":has(.schedule-tv-search-input:not(:placeholder-shown)) " +
        ".schedule-tv-search-icon::after {",
    );
  });

  it("folds the filter labels from level 2 onwards", () => {
    expect(FLAT).toContain(`.schedule-toolbar:is(${levelsFrom(2).join(",")})`);
  });

  it("folds the view labels at every level but the first", () => {
    // `[data-fit]` FIRST, then the negation: a bare `:not([data-fit="0"])`
    // matches a toolbar with no attribute at all, which is the flag-off page.
    expect(SCHEDULE_CSS).toContain(
      '.schedule-toolbar[data-fit]:not([data-fit="0"]) .schedule-view-seg .schedule-fit-lbl',
    );
  });

  it("draws the folded New task button as a square, not a padded pill", () => {
    const at = FLAT.indexOf(`.schedule-toolbar:is(${levelsFrom(4).join(",")}) .schedule-new {`);
    expect(at).toBeGreaterThan(-1);
    const block = FLAT.slice(at).slice(0, 400);
    expect(block).toContain("width: 32px");
    expect(block).toContain("min-width: 0");
  });

  it("HIDES the controls in the order Akshil set, lowest priority first", () => {
    // Project (by merging, so its rows stay reachable), then Status, then
    // Calendar, Cards, Board (design.md, Widths v2 as reordered by Polish
    // batch 4 — the search is no longer one of these; it folds at rung 2 and
    // stays). Each rung's rule must hold at its own level and at every level
    // past it.
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(6).join(",")}) .schedule-tv-filters .schedule-tv-pop-wrap`,
    );
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(7).join(",")}) .schedule-view-btn[data-view="calendar"]`,
    );
    expect(FLAT).toContain(
      `.schedule-toolbar:is(${levelsFrom(8).join(",")}) .schedule-view-btn[data-view="cards"]`,
    );
    expect(FLAT).toContain('.schedule-toolbar[data-fit="9"] .schedule-view-btn[data-view="board"]');
  });

  it("never hides List, and never hides New task", () => {
    // The two controls the page cannot be without: its default view, and the
    // one control on the row that STARTS something. No rung may name either.
    expect(FLAT).not.toContain('.schedule-view-btn[data-view="list"] { display: none');
    expect(TOOLBAR_DROPS.some((sel) => sel.includes('data-view="list"'))).toBe(false);
    expect(TOOLBAR_DROPS.some((sel) => /\.schedule-new$/.test(sel))).toBe(false);
  });

  it("NEVER wraps — the fallback is gone (design.md, Widths v2)", () => {
    // The toolbar is exempt from the middle pane's floor and stays on one line
    // at every width; the ladder now runs down to List and "+".
    expect(SCHEDULE_CSS).toContain("flex-wrap: nowrap");
    expect(SCHEDULE_CSS).not.toContain("data-wrap");
  });
});
