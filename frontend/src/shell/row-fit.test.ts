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
  naturalNeed,
  pickRowLevel,
  rowNeed,
} from "./row-fit";

// age, count, project chip, Draft chip — plausible measured costs.
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

describe("the toolbar's fold rules, in the stylesheet", () => {
  // The ladder is half arithmetic and half CSS, and the CSS half has its own
  // failure: a level named once and then overtaken puts the words back on. The
  // New task label did exactly that — folded at level 3 and BACK at level 4,
  // which is the one level it most needed to be gone (measured live).
  it("folds the New task label at level 3 AND at every level past it", () => {
    expect(SCHEDULE_CSS).toContain(
      '.schedule-toolbar:is([data-fit="3"], [data-fit="4"]) .schedule-new .schedule-fit-lbl',
    );
  });

  it("folds the filter labels from level 2 onwards", () => {
    expect(SCHEDULE_CSS).toContain(
      '.schedule-toolbar:is([data-fit="2"], [data-fit="3"], [data-fit="4"])',
    );
  });

  it("folds the view labels at every level but the first", () => {
    // `[data-fit]` FIRST, then the negation: a bare `:not([data-fit="0"])`
    // matches a toolbar with no attribute at all, which is the flag-off page.
    expect(SCHEDULE_CSS).toContain(
      '.schedule-toolbar[data-fit]:not([data-fit="0"]) .schedule-view-seg .schedule-fit-lbl',
    );
  });

  it("draws the folded New task button as a square, not a padded pill", () => {
    const block = SCHEDULE_CSS.slice(
      SCHEDULE_CSS.indexOf('.schedule-toolbar[data-fit="3"] .schedule-new,'),
    ).slice(0, 900);
    expect(block).toContain("width: 32px");
    expect(block).toContain("min-width: 0");
  });

  it("wraps ONLY when the ladder is spent, never by default", () => {
    // `flex-wrap` wraps before it shrinks, so a row that wraps by default never
    // spends the search box's slack and the ladder stops meaning anything.
    expect(SCHEDULE_CSS).toContain("flex-wrap: nowrap");
    expect(SCHEDULE_CSS).toContain('.schedule-toolbar[data-fit][data-wrap="1"] {');
    const wrapBlock = SCHEDULE_CSS.slice(
      SCHEDULE_CSS.indexOf('.schedule-toolbar[data-fit][data-wrap="1"] {'),
    ).slice(0, 120);
    expect(wrapBlock).toContain("flex-wrap: wrap");
    expect(wrapBlock).toContain("row-gap: 8px");
  });
});
