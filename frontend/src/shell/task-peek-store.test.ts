// The task side peek's rules, executed rather than grepped
// (.claude-design/task-side-peek/design.md). Four of them are arithmetic and
// one is a state machine, and every one is a rule a screenshot cannot check:
//
//   * the width clamp, including the case where the content area falls below
//     the panel's own minimum (a narrow window) — a `NaN` or a negative here
//     becomes an inline `width: NaNpx` on the panel;
//   * the baseline arithmetic: the default that keeps the column exactly where
//     it was, and the ¾ floor the middle pane stops shrinking at;
//   * the `?peek=` codec, which must carry the view and the filters through
//     untouched;
//   * the prev/next walk, which must NOT wrap;
//   * the sidebar's CROSSING DETECTOR — a state machine, and the one rule here
//     a screenshot could never check: it fires on a change of side and on
//     nothing else, so an open, a close or a swap moves no sidebar and a
//     reader's own chevron stands until the floor is crossed the other way;
//   * the store: who may open a peek at all (nobody, off the Tasks page).
import { SIDE_PANE_MIN_WIDTH } from "@platform/lib/pane-metrics";
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";

// bun has neither storage, and BOTH of this store's persisted facts are round
// trips through one — the dragged width, and the marker that says a collapsed
// sidebar is ours to hand back after a reload. Real (tiny) stores rather than
// spies, for the reason dismiss-store.test.ts gives: what is being checked is
// the round trip. `??=` so a suite that installed its own first keeps it —
// every file in a bun run shares one `globalThis`.
function tinyStorage() {
  const map = new Map<string, string>();
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, String(v)),
    removeItem: (k: string) => void map.delete(k),
    clear: () => map.clear(),
  };
}
const g = globalThis as { localStorage?: unknown; sessionStorage?: unknown };
g.localStorage ??= tinyStorage();
g.sessionStorage ??= tinyStorage();

const sidebar = await import("@platform/lib/sidebarstate");

const {
  PEEK_AUTOCOLLAPSE_KEY,
  PEEK_COVER_FLOOR,
  PEEK_MIN_WIDTH,
  PEEK_WIDTH_KEY,
  SIDEBAR_HYSTERESIS,
  clampPeekWidth,
  defaultPeekWidth,
  middleFloor,
  peekWidthFor,
  closePeek,
  getPeekState,
  openPeek,
  peekHostReady,
  peekSearch,
  nextAfterRemoval,
  openTimeCollapse,
  planCrossing,
  storableWidth,
  tasksBaselineFrom,
  planRoom,
  readPeekParam,
  applyResize,
  currentRoom,
  measureTasksBaseline,
  peekGutter,
  setPeekBaselineCandidate,
  refreshPeekBaseline,
  resetPeekStoreForTests,
  resetPeekWidth,
  frameClickCloses,
  resolvePeekKey,
  settlePeek,
  setPeekHost,
  setPeekWidth,
  showListBesidePeek,
  canShowList,
  planShowList,
  arrowShouldWalk,
  peekItemProps,
  peekScrollTarget,
  peekVisibleOrder,
  stepPeekKey,
  syncPeekFromUrl,
} = await import("./task-peek-store");

beforeEach(() => {
  resetPeekStoreForTests();
  sidebar.setSidebarState({ width: 232, collapsed: false });
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* no storage in this runtime — every write is already best-effort */
  }
});

/** THE ONE WINDOW every store-level sidebar test below is set in: 1538 wide,
 *  a 232 sidebar, so 1306 of content — and a measured baseline of 1006, which
 *  puts the middle pane's floor at 755 and the crossing somewhere around a
 *  551px peek. Wide enough that nothing is at a clamp, which is the point: what
 *  is being checked is the CROSSING, not an edge. */
const VIEWPORT = 1538;
/** The sidebar's rail, from the module that owns it, so the arithmetic below
 *  reads as arithmetic and cannot drift from it. */
const SIDEBAR_RAIL = sidebar.SIDEBAR_RAIL_WIDTH;
/** A baseline that leaves the panel MORE than its minimum on this window —
 *  1306 of content less 906 is 400, exactly the floor — so "the first open
 *  keeps the column where it was" is a statement about the arithmetic and not
 *  about a clamp. It moved from 1006 when the minimum went from 220 to 400
 *  (platform/lib/pane-metrics.ts). */
const BASELINE = 906;
/** 906 × ¾, rounded — stated here so a test reads as arithmetic rather than as
 *  a magic number, and pinned against the implementation below. */
const FLOOR = 680;

function windowWidth(px: number): void {
  (globalThis as { window: { innerWidth: number } }).window.innerWidth = px;
}

const named = (key: string, task_id: string) => ({ key, task_id });

describe("the width", () => {
  it("opens at whatever the middle pane's baseline does not need", () => {
    // THE RULE THAT REPLACED THE 50% DEFAULT (design.md, Widths v2): the panel
    // is the REMAINDER, so the column it opened beside is left exactly where it
    // was rather than being halved by a panel with no opinion about it.
    expect(defaultPeekWidth(2000, BASELINE)).toBe(2000 - BASELINE);
    expect(peekWidthFor(null, 2000, BASELINE)).toBe(2000 - BASELINE);
  });

  it("takes its minimum when there is no remainder to speak of", () => {
    // 1200 of content against a 906 baseline leaves 294 — under the panel's own
    // floor, so the panel takes its minimum and the middle pane is the one that
    // gives way (down to ITS floor, and then into an overflow).
    expect(defaultPeekWidth(1200, BASELINE)).toBe(PEEK_MIN_WIDTH);
  });

  it("falls back to the page's own column cap when nothing has been measured", () => {
    // A first paint, or a test with no DOM: the column can never be wider than
    // 1050 plus its gutters, so that is the most the baseline can be worth.
    expect(defaultPeekWidth(2000, null)).toBe(2000 - 1094);
    // …and on a window narrower than the cap the whole content area is the
    // column, so there is no remainder and the panel takes its minimum.
    expect(defaultPeekWidth(800, null)).toBe(PEEK_MIN_WIDTH);
  });

  it("floors the middle pane at three quarters of its baseline", () => {
    expect(middleFloor(BASELINE)).toBe(FLOOR);
    expect(middleFloor(1000)).toBe(750);
    expect(middleFloor(0)).toBe(0);
  });

  it("keeps a dragged width that fits", () => {
    expect(clampPeekWidth(700, 2000)).toBe(700);
    expect(peekWidthFor(700, 2000, BASELINE)).toBe(700);
  });

  it("floors at the Explorer Claude pane's minimum, which is now literally it", () => {
    // One constant for the two panes that are the same chat in the same shell
    // (platform/lib/pane-metrics.ts, which carries the derivation).
    expect(clampPeekWidth(100, 2000)).toBe(PEEK_MIN_WIDTH);
    expect(PEEK_MIN_WIDTH).toBe(SIDE_PANE_MIN_WIDTH);
  });

  it("has NO maximum but the content area itself", () => {
    // The old ⅔ cap is gone (design.md, Widths v2). What stops a drag from
    // swallowing the page is cover mode, which is a different STATE.
    expect(clampPeekWidth(1900, 2000)).toBe(1900);
    expect(clampPeekWidth(2500, 2000)).toBe(2000);
  });

  it("RE-CLAMPS a persisted width onto a window it no longer fits", () => {
    expect(peekWidthFor(1200, 900, BASELINE)).toBe(900);
  });

  it("keeps the floor when the content area falls below it", () => {
    // A 200px content area cannot hold a 220px panel; the panel is about to be
    // in cover mode anyway, where this number is not what renders.
    expect(clampPeekWidth(300, 200)).toBe(PEEK_MIN_WIDTH);
  });

  it("never answers NaN for a broken persisted value", () => {
    expect(clampPeekWidth(Number.NaN, 2000)).toBe(PEEK_MIN_WIDTH);
  });
});

describe("storableWidth — the cover trap", () => {
  // Bugbot, PR #1138. Dragging INTO cover is useful; reopening into it is a
  // dead end, because the seam is the only control that makes the panel
  // narrower and a covered page has no list to come back to.
  it("keeps the middle pane's cover floor out of the remembered number", () => {
    expect(storableWidth(1306, 1306)).toBe(1306 - PEEK_COVER_FLOOR);
    expect(storableWidth(1200, 1306)).toBe(1200 - 254);
  });

  it("leaves a width that was never near cover alone", () => {
    expect(storableWidth(600, 1306)).toBe(600);
  });

  it("still answers the panel's own minimum on a window that cannot help", () => {
    // 400 of content cannot hold a 220 panel AND a 360 middle pane. There the
    // page is in cover because of the window, not because of a number.
    expect(storableWidth(400, 400)).toBe(PEEK_MIN_WIDTH);
  });
});

describe("tasksBaselineFrom", () => {
  // Measured from the column's CAP, so the answer does not change when an open
  // panel narrows the rendered box (Bugbot, PR #1138).
  it("is the capped column plus its gutters on a wide page", () => {
    expect(tasksBaselineFrom(1050, 44, 1494)).toBe(1094);
    // …and the same answer with a panel already taking half the area, which is
    // the whole point: a deep link measures with the peek open.
    expect(tasksBaselineFrom(1050, 44, 1494)).toBe(tasksBaselineFrom(1050, 44, 1494));
  });

  it("is the room itself when the room is narrower than the cap", () => {
    expect(tasksBaselineFrom(1050, 44, 600)).toBe(600);
  });

  it("falls back to the room when the page states no cap", () => {
    expect(tasksBaselineFrom(Number.NaN, 44, 900)).toBe(900);
  });
});

describe("the ?peek= codec", () => {
  it("reads the key a deep link names", () => {
    expect(readPeekParam("?peek=sess-7&view=board")).toBe("sess-7");
  });

  it("reads null when there is none, and for an empty one", () => {
    expect(readPeekParam("?view=board")).toBeNull();
    expect(readPeekParam("?peek=")).toBeNull();
  });

  it("sets the key and leaves every other param where it was", () => {
    expect(peekSearch("?view=board&project=x", "sess-7")).toBe(
      "?view=board&project=x&peek=sess-7",
    );
  });

  it("replaces the key on a swap rather than appending a second one", () => {
    expect(peekSearch("?peek=a&view=board", "b")).toBe("?peek=b&view=board");
  });

  it("removes it on close, keeping the lens the reader set", () => {
    expect(peekSearch("?peek=a&view=board", null)).toBe("?view=board");
  });

  it("answers an empty string when nothing is left, so a bare path stays bare", () => {
    expect(peekSearch("?peek=a", null)).toBe("");
  });
});

describe("stepPeekKey", () => {
  const order = ["a", "b", "c"];

  it("walks the visible order", () => {
    expect(stepPeekKey(order, "a", 1)).toBe("b");
    expect(stepPeekKey(order, "c", -1)).toBe("b");
  });

  it("does NOT wrap at either end", () => {
    expect(stepPeekKey(order, "c", 1)).toBeNull();
    expect(stepPeekKey(order, "a", -1)).toBeNull();
  });

  it("has nowhere to go from a task the view is not showing", () => {
    // Filtered away while the peek held it: the arrows go quiet rather than
    // jumping to whatever happens to be first.
    expect(stepPeekKey(order, "z", 1)).toBeNull();
    expect(stepPeekKey(order, null, 1)).toBeNull();
  });
});

describe("peekVisibleOrder — the walk skips what the panel cannot open", () => {
  // The items as the walk reads them: nodes carrying the attributes, in the
  // order the view painted them. A draft row still carries its KEY (it is an
  // item, and the frame's click-to-close must keep sparing it) and carries the
  // skip mark beside it — design.md, Fix batch 6 §3.
  const node = (props: Record<string, string>) => ({
    getAttribute: (name: string) => props[name] ?? null,
  });
  const item = (key: string, openable = true) => node(peekItemProps(key, openable));

  it("stamps the skip only on an item the panel cannot open", () => {
    expect(peekItemProps("a", true)).toEqual({ "data-peek-key": "a" });
    expect(peekItemProps("a", false)).toEqual({
      "data-peek-key": "a",
      "data-peek-skip": "1",
    });
  });

  it("leaves the drafts out of the order", () => {
    const order = peekVisibleOrder([
      item("a"),
      item("draft-1", false),
      item("b"),
      item("draft-2", false),
      item("c"),
    ]);
    expect(order).toEqual(["a", "b", "c"]);
  });

  it("steps OVER a draft that sits between two tasks", () => {
    // The row is still on the page and still opens its own form on a press —
    // what changes is that ↓ no longer stops on it and asks to be pressed again.
    const order = peekVisibleOrder([item("a"), item("draft-1", false), item("b")]);
    expect(stepPeekKey(order, "a", 1)).toBe("b");
    expect(stepPeekKey(order, "b", -1)).toBe("a");
  });

  it("advances past a draft after an archive", () => {
    // The panel does not close on an archive — it moves to the next task DOWN
    // (design.md, Header + list state v2), and "next" means the next one it can
    // actually show.
    const order = peekVisibleOrder([item("a"), item("draft-1", false), item("b")]);
    expect(nextAfterRemoval(order, "a")).toBe("b");
    // …and at the end of the list, the previous one — never the draft.
    expect(nextAfterRemoval(order, "b")).toBe("a");
  });

  it("closes when the drafts are all that is left", () => {
    const order = peekVisibleOrder([item("a"), item("draft-1", false)]);
    expect(nextAfterRemoval(order, "a")).toBeNull();
    expect(stepPeekKey(order, "a", 1)).toBeNull();
  });

  it("still takes each key once, whatever the view painted twice", () => {
    const order = peekVisibleOrder([item("a"), item("a"), item("b")]);
    expect(order).toEqual(["a", "b"]);
  });
});

describe("peekScrollTarget — which element a walk brings back", () => {
  // The items as the walk reads them: nodes carrying the attribute, in the
  // order the view painted them. Only `getAttribute` is ever asked for, which
  // is why this is provable without a browser.
  const item = (key: string) => ({
    key,
    getAttribute: (name: string) => (name === "data-peek-key" ? key : null),
  });
  const items = [item("a"), item("b"), item("c")];

  it("is the item the walk landed on", () => {
    expect(peekScrollTarget(items, "b")?.key).toBe("b");
  });

  it("takes the FIRST node a key is painted on", () => {
    // A view may paint one task twice — a board card and its drag ghost — and
    // the walk means places, not nodes, exactly as `visibleOrder` does when it
    // reads these same elements.
    const twice = [item("a"), item("b"), { ...item("b"), key: "ghost" }];
    expect(peekScrollTarget(twice, "b")?.key).toBe("b");
  });

  it("is nothing when the view has not painted the task", () => {
    // Filtered away between the press and the paint: scroll nothing rather than
    // guess at a neighbour.
    expect(peekScrollTarget(items, "z")).toBeNull();
    expect(peekScrollTarget(items, null)).toBeNull();
    expect(peekScrollTarget([], "a")).toBeNull();
  });

  it("never splices a key into a selector", () => {
    // Task keys are paths and session ids — arbitrary strings, which have no
    // business inside a CSS attribute selector. A key full of quotes is just a
    // string comparison here.
    const odd = 'a"b\\c';
    expect(peekScrollTarget([item(odd)], odd)?.key).toBe(odd);
  });
});

describe("arrowShouldWalk — who owns ↑/↓", () => {
  // A pure guard over the focused element (design.md, Polish batch 4). Plain
  // objects rather than a rendered tree: what is being checked is the rule, and
  // the three properties it reads are the whole of its input.
  const el = (
    tagName: string,
    opts: { editable?: boolean; inside?: string } = {},
  ): EventTarget =>
    ({
      tagName,
      isContentEditable: !!opts.editable,
      closest: (sel: string) => (opts.inside && sel.includes(opts.inside) ? {} : null),
    }) as unknown as EventTarget;

  it("walks for the chrome the panel and the page are made of", () => {
    for (const tag of ["BUTTON", "DIV", "A", "SPAN", "LI", "BODY"]) {
      expect(arrowShouldWalk(el(tag))).toBe(true);
    }
    // A press with nothing focused lands on the document, which has no
    // `closest` — nobody has a prior claim, so the panel takes it.
    expect(arrowShouldWalk(null)).toBe(true);
    expect(arrowShouldWalk({} as EventTarget)).toBe(true);
  });

  it("stands down inside anything that types", () => {
    // An ↑ in a composer moves the caret and in a search field walks the
    // history; taking it would move the reader's place without losing anything,
    // which is the quiet version of the Escape bug this panel has already had.
    for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
      expect(arrowShouldWalk(el(tag))).toBe(false);
    }
    expect(arrowShouldWalk(el("DIV", { editable: true }))).toBe(false);
  });

  it("stands down on a focused frame — the arrows there are the app's", () => {
    // The app preview and the legacy chat are other documents; this is the case
    // where the element holding focus in OURS is the frame itself.
    expect(arrowShouldWalk(el("IFRAME"))).toBe(false);
  });

  it("stands down inside an open menu, where ↑/↓ already walk rows", () => {
    // Including the kebab's own menu, which is opened from this very header.
    expect(arrowShouldWalk(el("DIV", { inside: ".context-menu" }))).toBe(false);
    expect(arrowShouldWalk(el("BUTTON", { inside: ".tasks-pop" }))).toBe(false);
    expect(arrowShouldWalk(el("DIV", { inside: '[role="menu"]' }))).toBe(false);
  });

  it("stands down inside a DIALOG — a modal is modal (Bugbot, 78118e0fa)", () => {
    // The worst case of all, and the one that found this: the delete
    // confirmation's Confirm and Cancel are plain buttons, so an arrow pressed
    // in it went straight past them and swapped the panel's task behind a
    // dialog still naming the old one. All three marks the shared `Modal`
    // chassis puts on a dialog, so one that wears only one is still covered.
    expect(arrowShouldWalk(el("BUTTON", { inside: ".modal-dialog" }))).toBe(false);
    expect(arrowShouldWalk(el("BUTTON", { inside: '[role="dialog"]' }))).toBe(false);
    expect(arrowShouldWalk(el("DIV", { inside: '[aria-modal="true"]' }))).toBe(false);
  });

  it("stands down over the native chat transcript — those arrows scroll it (Bugbot, 3b720ee0b)", () => {
    // The native chat lives in THIS document, so the frame guard at the call
    // site does not see it; ↑/↓ over a turn belong to the transcript's scroll.
    expect(arrowShouldWalk(el("DIV", { inside: ".task-side-peek-chat" }))).toBe(false);
    // The panel's header is not the transcript: chevrons and title still walk.
    expect(arrowShouldWalk(el("DIV", { inside: ".task-side-peek-head" }))).toBe(true);
  });
});

describe("nextAfterRemoval", () => {
  const order = ["a", "b", "c"];

  it("goes DOWN the list, because filing is a sweep", () => {
    expect(nextAfterRemoval(order, "a")).toBe("b");
    expect(nextAfterRemoval(order, "b")).toBe("c");
  });

  it("falls back to the previous one at the end of the list", () => {
    expect(nextAfterRemoval(order, "c")).toBe("b");
  });

  it("answers null — CLOSE — only when there is genuinely nothing left", () => {
    expect(nextAfterRemoval(["only"], "only")).toBeNull();
    expect(nextAfterRemoval([], "a")).toBeNull();
    // A key the view is not showing is not "the last one", it is not in the
    // list at all, and guessing a neighbour for it would open a conversation
    // nobody asked for.
    expect(nextAfterRemoval(order, "z")).toBeNull();
  });
});

describe("planRoom", () => {
  const open = {
    open: true,
    viewport: VIEWPORT,
    chosenWidth: null as number | null,
    baseline: BASELINE,
    sidebarWidth: 232,
  };

  it("leaves the column EXACTLY where it was on a first open", () => {
    // The whole of Widths v2's first rule: 1306 of content, a 906 baseline, so
    // the panel takes the 400 that is left over and the middle pane keeps the
    // width it was already being read at.
    const plan = planRoom(open);
    expect(plan.peekWidth).toBe(400);
    expect(plan.frameAfter).toBe(BASELINE);
    expect(plan.floored).toBe(false);
    expect(plan.cover).toBe(false);
    expect(plan.floor).toBe(FLOOR);
  });

  it("reflows, without flooring, while the middle pane is above three quarters", () => {
    const plan = planRoom({ ...open, chosenWidth: 500 });
    expect(plan.frameAfter).toBe(806);
    expect(plan.floored).toBe(false);
  });

  it("FLOORS the middle pane once it would drop under three quarters", () => {
    // 1306 − 700 = 606, under the 680 floor: the rows stop shrinking and the
    // pane scrolls sideways instead.
    const plan = planRoom({ ...open, chosenWidth: 700 });
    expect(plan.frameAfter).toBe(606);
    expect(plan.floored).toBe(true);
    expect(plan.contentFloor).toBe(FLOOR);
    expect(plan.cover).toBe(false);
  });

  it("covers once the middle pane would drop under 360", () => {
    // 1306 − 1000 = 306 < 360. The panel is then honestly the whole content
    // area rather than a sliver of list pretending to still be a view.
    const plan = planRoom({ ...open, chosenWidth: 1000 });
    expect(plan.cover).toBe(true);
    expect(plan.peekWidth).toBe(1306);
    expect(plan.frameAfter).toBe(1306);
    // Cover is not "floored": there is no middle pane on screen to scroll.
    expect(plan.floored).toBe(false);
  });

  it("lets the panel take everything, with no ⅔ cap in the way", () => {
    const plan = planRoom({ ...open, chosenWidth: 5000 });
    expect(plan.peekWidth).toBe(1306);
    expect(plan.cover).toBe(true);
  });

  it("gives a closed peek the whole content area and floors nothing", () => {
    const plan = planRoom({ ...open, open: false, chosenWidth: 1000 });
    expect(plan.frameAfter).toBe(1306);
    expect(plan.floored).toBe(false);
    expect(plan.cover).toBe(false);
  });
});

describe("openTimeCollapse — the ONE thing an open may do to the sidebar", () => {
  const at = (viewport: number) => ({
    viewport,
    baseline: BASELINE,
    sidebarExpanded: 232,
    sidebarCollapsed: false,
  });

  it("leaves a wide window alone: there is already room for a full peek", () => {
    // 1538 − 232 = 1306 of content, less a 906 baseline → exactly the minimum.
    expect(openTimeCollapse(at(VIEWPORT))).toBe(false);
  });

  it("COLLAPSES when the column and a minimum peek will not both fit", () => {
    // 1400 − 232 = 1168, less 906 → 262 spare, under the panel's minimum. The
    // rail gives 1400 − 44 = 1356, less 906 → 450. Worth spending.
    expect(openTimeCollapse(at(1400))).toBe(true);
  });

  it("does NOT move a sidebar the reader has already collapsed", () => {
    expect(openTimeCollapse({ ...at(1400), sidebarCollapsed: true })).toBe(false);
  });

  it("does not move it for NOTHING — a window too narrow either way", () => {
    // 1200 − 44 = 1156, less 906 → 250, still under the minimum. The collapse
    // would buy a panel that still cannot have its minimum, so the middle pane
    // gives way exactly as it did before and the sidebar stays put.
    expect(openTimeCollapse(at(1200))).toBe(false);
  });

  it("reads the sidebar's EXPANDED width, whatever it has been dragged to", () => {
    // A reader who widened their sidebar to 400 hits the condition sooner.
    expect(openTimeCollapse({ ...at(1500), sidebarExpanded: 400 })).toBe(true);
    expect(openTimeCollapse({ ...at(1500), sidebarExpanded: 180 })).toBe(false);
  });
});

describe("planCrossing", () => {
  // Everything measured with the sidebar EXPANDED, which is the whole reason
  // the expand cannot be re-triggered into a collapse: one variable, one floor.
  const at = (peek: number) => ({
    viewport: VIEWPORT,
    chosenWidth: peek,
    baseline: BASELINE,
    sidebarExpanded: 232,
    sidebarCollapsed: false,
    side: "above" as "above" | "below" | null,
  });

  it("establishes a side on the first read and fires nothing", () => {
    // What an OPEN does. An open is not a resize (design.md, Widths v2).
    const plan = planCrossing({ ...at(400), side: null });
    expect(plan.sidebar).toBeNull();
    expect(plan.side).toBe("above");
    expect(plan.middleIfExpanded).toBe(906);
  });

  it("collapses on a DOWNWARD crossing", () => {
    const plan = planCrossing(at(700));
    expect(plan.middleIfExpanded).toBe(606);
    expect(plan.sidebar).toBe(true);
    expect(plan.side).toBe("below");
  });

  it("collapses even a sidebar the reader opened deliberately", () => {
    // The one place the floor rule overrules them, and Akshil asked for it by
    // name: the middle pane is about to overflow and the 188px is the only
    // room left to give it.
    const plan = planCrossing({ ...at(700), side: "above", sidebarCollapsed: false });
    expect(plan.sidebar).toBe(true);
  });

  it("asks for nothing when the sidebar is already where the crossing wants it", () => {
    expect(planCrossing({ ...at(700), sidebarCollapsed: true }).sidebar).toBeNull();
    expect(planCrossing({ ...at(400), side: "below", sidebarCollapsed: false }).sidebar).toBeNull();
  });

  it("expands on an UPWARD crossing", () => {
    const plan = planCrossing({ ...at(400), side: "below", sidebarCollapsed: true });
    expect(plan.sidebar).toBe(false);
    expect(plan.side).toBe("above");
  });

  it("does NOTHING while the pane stays on the side it was already on", () => {
    // Narrower still, below the floor throughout: no second crossing, so a
    // sidebar the reader re-opened in between is simply left alone.
    const plan = planCrossing({ ...at(800), side: "below", sidebarCollapsed: false });
    expect(plan.sidebar).toBeNull();
    expect(plan.side).toBe("below");
  });

  it("holds the reader's override until the floor is crossed the OTHER way", () => {
    // They re-opened the sidebar while below the floor. Resizing about below
    // the floor changes nothing…
    expect(planCrossing({ ...at(750), side: "below", sidebarCollapsed: false }).sidebar).toBeNull();
    // …until the pane comes back up (nothing to do, it is already open)…
    const up = planCrossing({ ...at(400), side: "below", sidebarCollapsed: false });
    expect(up.sidebar).toBeNull();
    expect(up.side).toBe("above");
    // …and then goes down again, which IS a crossing and does collapse it.
    expect(planCrossing({ ...at(700), side: up.side, sidebarCollapsed: false }).sidebar).toBe(true);
  });

  it("holds a manual CLOSE until the floor is crossed upward", () => {
    // They collapsed it themselves while above the floor. Widening is not a
    // crossing, so it stays shut…
    expect(planCrossing({ ...at(350), side: "above", sidebarCollapsed: true }).sidebar).toBeNull();
    // …and it only comes back when the pane actually crosses up from below.
    expect(planCrossing({ ...at(400), side: "below", sidebarCollapsed: true }).sidebar).toBe(false);
  });

  it("treats COVER as below the floor, and coming back out as a crossing up", () => {
    // Dragged until the middle pane is gone: `middleIfExpanded` goes to nothing
    // (or past it), which is emphatically below the floor.
    const covered = planCrossing({ ...at(1306), side: "above" });
    expect(covered.middleIfExpanded).toBeLessThanOrEqual(0);
    expect(covered.side).toBe("below");
    expect(covered.sidebar).toBe(true);
    // …and back out: an upward crossing, which hands the sidebar back.
    const out = planCrossing({ ...at(400), side: "below", sidebarCollapsed: true });
    expect(out.side).toBe("above");
    expect(out.sidebar).toBe(false);
    // …unless the reader had opened it again in between, in which case there is
    // nothing to hand back and the crossing asks for nothing.
    expect(
      planCrossing({ ...at(400), side: "below", sidebarCollapsed: false }).sidebar,
    ).toBeNull();
  });

  it("keeps a 16px band around the floor so the seam cannot make it flap", () => {
    expect(SIDEBAR_HYSTERESIS).toBe(16);
    // Middle pane at exactly the floor, coming up from below: inside the band,
    // so it is still "below" and the sidebar is not handed back yet.
    const inBand = planCrossing({
      ...at(VIEWPORT - 232 - FLOOR),
      side: "below",
      sidebarCollapsed: true,
    });
    expect(inBand.middleIfExpanded).toBe(FLOOR);
    expect(inBand.side).toBe("below");
    expect(inBand.sidebar).toBeNull();
    // 16px past it, and it is.
    const past = planCrossing({
      ...at(VIEWPORT - 232 - FLOOR - SIDEBAR_HYSTERESIS),
      side: "below",
      sidebarCollapsed: true,
    });
    expect(past.middleIfExpanded).toBe(FLOOR + SIDEBAR_HYSTERESIS);
    expect(past.sidebar).toBe(false);
  });
});

describe("the store", () => {
  it("declines to open anywhere the Tasks page is not hosting a peek", () => {
    expect(peekHostReady()).toBe(false);
    expect(openPeek("sess-1")).toBe(false);
    expect(getPeekState().key).toBeNull();
  });

  it("opens, swaps and closes once the page is hosting", () => {
    setPeekHost(true);
    expect(openPeek("sess-1")).toBe(true);
    expect(getPeekState().key).toBe("sess-1");
    // A swap keeps the panel where it is — `instant` stays false, which is what
    // says "no re-slide" to the CSS.
    expect(openPeek("sess-2")).toBe(true);
    expect(getPeekState().key).toBe("sess-2");
    expect(getPeekState().instant).toBe(false);
    closePeek();
    expect(getPeekState().key).toBeNull();
  });

  it("treats re-opening the task already in the panel as a no-op", () => {
    setPeekHost(true);
    openPeek("sess-1");
    const before = getPeekState();
    expect(openPeek("sess-1")).toBe(true);
    expect(getPeekState()).toBe(before);
  });

  it("refuses an empty key", () => {
    setPeekHost(true);
    expect(openPeek("")).toBe(false);
  });

  it("closes when the page goes away — navigating away is a close trigger", () => {
    setPeekHost(true);
    openPeek("sess-1");
    setPeekHost(false);
    expect(getPeekState().key).toBeNull();
    // …and the peek does not spring back the next time the page mounts.
    setPeekHost(true);
    expect(getPeekState().key).toBeNull();
  });

  it("follows the URL on a traversal, without animating", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-9");
    expect(getPeekState().key).toBe("sess-9");
    expect(getPeekState().instant).toBe(true);
    syncPeekFromUrl("?view=board");
    expect(getPeekState().key).toBeNull();
  });

  it("ignores the URL off the Tasks page", () => {
    syncPeekFromUrl("?peek=sess-9");
    expect(getPeekState().key).toBeNull();
  });

  it("pushes ONE entry per open, per swap and per close", () => {
    const h = globalThis.history as { pushState: (...a: unknown[]) => void };
    const loc = globalThis.location as { pathname: string; search: string };
    const real = h.pushState;
    const wasSearch = loc.search;
    let pushes = 0;
    // A push that MOVES the address, because the store declines to push an
    // entry identical to the one it is standing on — a real guard, and one a
    // frozen `location` would hide.
    h.pushState = (_state: unknown, _title: unknown, url: unknown) => {
      pushes += 1;
      const q = String(url).indexOf("?");
      loc.search = q === -1 ? "" : String(url).slice(q);
    };
    try {
      setPeekHost(true);
      openPeek("sess-1");
      expect(pushes).toBe(1);
      openPeek("sess-2");
      expect(pushes).toBe(2);
      closePeek();
      expect(pushes).toBe(3);
      // …and NONE when the caller is about to navigate itself ("Open as page"):
      // a `/tasks` entry pushed a tick before the Explorer's would make one
      // Back land on the Tasks page with the panel already gone.
      openPeek("sess-3");
      pushes = 0;
      closePeek({ push: false });
      expect(pushes).toBe(0);
    } finally {
      h.pushState = real;
      loc.search = wasSearch;
    }
  });

  it("keeps the width the reader dragged, and forgets it on a reset", () => {
    expect(getPeekState().width).toBeNull(); // no choice yet — half the area
    setPeekWidth(700);
    expect(getPeekState().width).toBe(700);
    resetPeekWidth();
    expect(getPeekState().width).toBeNull();
  });
});

describe("resolvePeekKey", () => {
  const tasks = [named("sess-a", "TASK-001"), named("sess-b", "TASK-002")];

  it("takes a row key as it stands", () => {
    expect(resolvePeekKey("sess-b", tasks)).toBe("sess-b");
  });

  it("resolves a task NUMBER to that task's key, case-insensitively", () => {
    expect(resolvePeekKey("TASK-002", tasks)).toBe("sess-b");
    expect(resolvePeekKey("task-002", tasks)).toBe("sess-b");
  });

  it("refuses an AMBIGUOUS number — two projects can both hold a TASK-007", () => {
    const twins = [named("sess-x", "TASK-007"), named("sess-y", "TASK-007")];
    expect(resolvePeekKey("TASK-007", twins)).toBeNull();
  });

  it("answers null for a key that names nothing, and for nothing", () => {
    expect(resolvePeekKey("sess-gone", tasks)).toBeNull();
    expect(resolvePeekKey(null, tasks)).toBeNull();
    expect(resolvePeekKey("sess-a", [])).toBeNull();
  });
});

describe("settlePeek", () => {
  it("CLOSES a peek whose key names no task, rather than standing open and empty", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-gone");
    expect(getPeekState().key).toBe("sess-gone");
    settlePeek([named("sess-a", "TASK-001")]);
    expect(getPeekState().key).toBeNull();
  });

  it("rewrites a task NUMBER to that task's row key in place", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=TASK-002");
    settlePeek([named("sess-a", "TASK-001"), named("sess-b", "TASK-002")]);
    expect(getPeekState().key).toBe("sess-b");
  });

  it("leaves a key that already names a task alone", () => {
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-a");
    const before = getPeekState();
    settlePeek([named("sess-a", "TASK-001")]);
    expect(getPeekState()).toBe(before);
  });

  it("says nothing while no peek is open", () => {
    setPeekHost(true);
    settlePeek([]);
    expect(getPeekState().key).toBeNull();
  });
});

describe("the sidebar, and the only thing that moves it", () => {
  /** Arm the page at the one window every test here uses, with a measured
   *  baseline, and open a peek on it. Returns nothing: what each test asserts
   *  is what the sidebar did (or, mostly, did not) next. */
  function arm(): void {
    windowWidth(VIEWPORT);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
  }

  it("does NOT move it on an open, however narrow the middle pane lands", () => {
    // The rule of 2026-09-14 (design.md, Widths v2): open/close/swap have no
    // say at all. A width the reader dragged last week is restored and the
    // sidebar is simply left where they had it.
    setPeekWidth(700);
    arm();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(getPeekState().autoCollapsed).toBe(false);
  });

  it("collapses it when a RESIZE takes the middle pane under its floor", () => {
    arm();
    setPeekWidth(700);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    expect(getPeekState().autoCollapsed).toBe(true);
    // WITHOUT WRITING THE READER'S PREFERENCE — an auto-collapse is the layout
    // getting out of the way, not the reader shutting the panel.
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("remembers across a reload that the collapse was ours", () => {
    arm();
    setPeekWidth(700);
    applyResize();
    let stored: string | null = null;
    try {
      stored = sessionStorage.getItem(PEEK_AUTOCOLLAPSE_KEY);
    } catch {
      /* no storage */
    }
    expect(stored).toBe("1");
  });

  it("puts it back when a resize brings the middle pane up again", () => {
    arm();
    setPeekWidth(700);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    setPeekWidth(400);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(getPeekState().autoCollapsed).toBe(false);
  });

  it("leaves the reader's own chevron alone until the floor is crossed again", () => {
    arm();
    setPeekWidth(700);
    applyResize();
    // They press the rail's chevron.
    sidebar.setSidebarState((s) => ({ ...s, collapsed: false }));
    expect(getPeekState().autoCollapsed).toBe(false);
    // Further narrowing, still below the floor: NOT a crossing, so it stands.
    setPeekWidth(750);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    // Up over the floor and back down under it — that IS one, and it collapses
    // even though they opened it deliberately.
    setPeekWidth(400);
    applyResize();
    setPeekWidth(700);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
  });

  it("PERSISTS a crossing that overrides the reader's own toggle", () => {
    // UX pass 2, 2026-09-14. They collapse it themselves (which persists), a
    // later upward crossing re-expands it — and if that write is silent the DOM
    // says 232 while `localStorage` still says `collapsed: true`, so the next
    // reload snaps the sidebar back to a rail with nothing on screen that did
    // it. The trigger overwrote a decision they can see; it has to be written
    // down like one.
    arm();
    setPeekWidth(700);
    applyResize();
    // They shut it themselves while the pane is below the floor.
    sidebar.setSidebarState((s) => ({ ...s, collapsed: true }));
    expect(sidebar.loadSidebarState().collapsed).toBe(true);
    // Back over the floor: an upward crossing, overriding THEIR toggle.
    setPeekWidth(400);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("…and stays silent when it is only overriding its OWN auto state", () => {
    // Nothing of the reader's is being overwritten here, so their preference is
    // left exactly as they last set it.
    arm();
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
    setPeekWidth(700);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
    setPeekWidth(400);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("undoes a PERSISTED collapse persistently, or the flip comes back", () => {
    // Found live, 2026-09-14: the collapse at (2) was written down because it
    // overrode their expand — and the expand at (3) was silent, because by then
    // nothing of theirs was being overridden. DOM 232, storage `true`, reload
    // → 44. What a write persists has to be decided by what it OVERWRITES, and
    // a collapse we wrote down is one of those things.
    arm();
    setPeekWidth(700);
    applyResize(); //  (1) our own collapse, silent
    sidebar.setSidebarState((s) => ({ ...s, collapsed: false })); // they expand
    setPeekWidth(400);
    applyResize();
    setPeekWidth(700);
    applyResize(); //  (2) overrides their expand → persisted collapse
    expect(sidebar.loadSidebarState().collapsed).toBe(true);
    setPeekWidth(400);
    applyResize(); //  (3) undoes (2) → must be persisted too
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("hands back a PERSISTED collapse the same way it was written", () => {
    arm();
    // They open it themselves…
    setPeekWidth(700);
    applyResize();
    sidebar.setSidebarState((s) => ({ ...s, collapsed: false }));
    // …and a later downward crossing overrides that, persistently.
    setPeekWidth(400);
    applyResize();
    setPeekWidth(700);
    applyResize();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    expect(sidebar.loadSidebarState().collapsed).toBe(true);
    // Leaving /tasks hands it back — and writes that down too, or the reload
    // puts the rail straight back.
    setPeekHost(false);
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
  });

  it("does NOT hand it back on an ordinary close", () => {
    // Closing is not a resize either (design.md, Widths v2). The sidebar stays
    // exactly where the last crossing left it; what puts it back is leaving
    // /tasks, below.
    arm();
    setPeekWidth(700);
    applyResize();
    closePeek();
    expect(sidebar.getSidebarState().collapsed).toBe(true);
  });

  it("hands it back when the page goes away", () => {
    arm();
    setPeekWidth(700);
    applyResize();
    // Navigating away from /tasks: there is no middle pane out here for the
    // floor rule to be about, so the reader must not be stranded with a rail
    // nothing on screen explains.
    setPeekHost(false);
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(getPeekState().autoCollapsed).toBe(false);
  });

  it("leaves a sidebar the reader collapsed themselves collapsed", () => {
    windowWidth(VIEWPORT);
    sidebar.setSidebarState({ width: 232, collapsed: true });
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    expect(getPeekState().autoCollapsed).toBe(false);
    setPeekHost(false);
    expect(sidebar.getSidebarState().collapsed).toBe(true);
  });

  it("forgets the baseline when the visit ends", () => {
    arm();
    expect(getPeekState().baseline).toBe(BASELINE);
    setPeekHost(false);
    expect(getPeekState().baseline).toBeNull();
  });

  it("does not latch a NULL baseline when a deep link beats the measurement", () => {
    // THE RACE (code review, 2026-09-14): `useTaskPeekHost` adopts `?peek=` in
    // a LAYOUT effect, and the page's baseline observer is a passive one — it
    // runs after. Nothing had been measured, so the freeze took null; and
    // because a freeze only ever retried on a FRESH open, the baseline stayed
    // null for the whole visit and the floor became a moving target.
    windowWidth(VIEWPORT);
    setPeekHost(true);
    // No candidate, and no DOM for the store's own read to fall back on.
    syncPeekFromUrl("?peek=sess-1");
    expect(getPeekState().key).toBe("sess-1");
    expect(getPeekState().baseline).toBeNull();
    // The observer lands a frame later…
    setPeekBaselineCandidate(BASELINE);
    // …and the next open — a SWAP, which is all a reader may do from here —
    // takes it. Once. The old shape never retried at all.
    openPeek("sess-2");
    expect(getPeekState().baseline).toBe(BASELINE);
    setPeekBaselineCandidate(700);
    openPeek("sess-3");
    expect(getPeekState().baseline).toBe(BASELINE);
  });

  it("takes the measurement the page offers before the link is adopted", () => {
    // The fix's own path: `useTaskPeekHost` calls `refreshPeekBaseline()` in
    // the same layout effect, BEFORE `syncPeekFromUrl`.
    windowWidth(VIEWPORT);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    refreshPeekBaseline();
    syncPeekFromUrl("?peek=sess-1");
    expect(getPeekState().baseline).toBe(BASELINE);
  });

  it("collapses the sidebar on a first open too narrow for a 220 peek", () => {
    // The ONE open-time exception (Akshil, 2026-09-14). 1400 − 232 = 1168
    // against a 1006 baseline leaves 162 — under the panel's minimum — and the
    // rail buys 350, so the chrome nobody is looking at gives way instead of
    // the column the reader is reading.
    windowWidth(1400);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    expect(sidebar.getSidebarState().collapsed).toBe(true);
    expect(getPeekState().autoCollapsed).toBe(true);
    // Ours, and not written to the reader's preference.
    expect(sidebar.loadSidebarState().collapsed).toBe(false);
    // …and the default width is re-derived against the area it actually got.
    expect(currentRoom().peekWidth).toBe(1400 - 44 - BASELINE);
    expect(currentRoom().frameAfter).toBe(BASELINE);
  });

  it("…and falls back to giving way when the collapse would not buy the 220", () => {
    windowWidth(1200);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    expect(sidebar.getSidebarState().collapsed).toBe(false);
    expect(getPeekState().autoCollapsed).toBe(false);
    // The middle pane gives way instead, exactly as it did before.
    expect(currentRoom().peekWidth).toBe(PEEK_MIN_WIDTH);
  });

  it("spends the open-time exception ONCE — a swap does not re-argue it", () => {
    windowWidth(1400);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    // The reader opens the sidebar again; a swap must not shut it.
    sidebar.setSidebarState((s) => ({ ...s, collapsed: false }));
    openPeek("sess-2");
    expect(sidebar.getSidebarState().collapsed).toBe(false);
  });

  it("adopts the first measurement even when the peek is ALREADY open", () => {
    // The deep-link case, end to end (Bugbot, PR #1138): the panel opens in a
    // layout effect, the page's own section arrives a tick later with the
    // tasks, and the observer's first reading has to become the baseline —
    // otherwise the visit runs on the fallback for good.
    windowWidth(VIEWPORT);
    setPeekHost(true);
    syncPeekFromUrl("?peek=sess-1");
    expect(getPeekState().baseline).toBeNull();
    setPeekBaselineCandidate(BASELINE);
    expect(getPeekState().baseline).toBe(BASELINE);
  });

  it("…but never lets a later reading move a baseline that is already frozen", () => {
    arm();
    expect(getPeekState().baseline).toBe(BASELINE);
    setPeekBaselineCandidate(700);
    expect(getPeekState().baseline).toBe(BASELINE);
  });

  it("never writes down a width that would reopen in cover", () => {
    arm();
    // A drag that ends in cover: what renders is the whole area, what is
    // remembered is held back by the middle pane's floor.
    setPeekWidth(5000);
    const stored = Number(localStorage.getItem(PEEK_WIDTH_KEY));
    expect(stored).toBeLessThanOrEqual(1306 - PEEK_COVER_FLOOR);
    // …and the next open comes back to a page with a view on it.
    closePeek();
    openPeek("sess-2");
    expect(currentRoom().cover).toBe(false);
  });

  it("re-clamps a remembered width against a window that has since shrunk", () => {
    // It was written safe on a wide desktop; the reader is on a narrower one
    // now — 1100 less a 232 sidebar is 868 of content, and a 1400 panel would
    // be cover with 532px of it to spare.
    setPeekWidth(1400);
    windowWidth(1100);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    expect(currentRoom().cover).toBe(false);
    expect(getPeekState().width).toBeLessThanOrEqual(868 - PEEK_COVER_FLOOR);
  });

  it("…and lets the window itself win when even the clamp cannot help", () => {
    // 900 less a 232 sidebar is 668 of content: a 400 panel and a 360 middle
    // pane do not both fit at any width, so cover is the honest answer rather
    // than a sliver of list nobody can read.
    setPeekWidth(1400);
    windowWidth(900);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    expect(getPeekState().width).toBe(PEEK_MIN_WIDTH);
    expect(currentRoom().cover).toBe(true);
  });

  it("comes back out of cover without closing the task", () => {
    // Akshil's option b (design.md, Polish batch 3): in cover the header's
    // first control says "Show list", and pressing it spends the split a fresh
    // open would — not a close, and not a seam nobody can find.
    arm();
    setPeekWidth(5000);
    expect(currentRoom().cover).toBe(true);
    const covering = getPeekState().key;
    showListBesidePeek();
    expect(currentRoom().cover).toBe(false);
    // The default width, which is the remainder past the middle pane's
    // baseline — the same number a first open would have taken.
    expect(getPeekState().width).toBe(1306 - BASELINE);
    expect(currentRoom().frameAfter).toBe(BASELINE);
    // …and the same task is still open.
    expect(getPeekState().key).toBe(covering);
  });

  it("…and leaves the middle pane clear of the cover floor on a tight window", () => {
    // A window where the default would itself be cover: the restore is held
    // back rather than handing the reader straight back into it.
    setPeekWidth(5000);
    windowWidth(1100);
    setPeekHost(true);
    setPeekBaselineCandidate(BASELINE);
    openPeek("sess-1");
    showListBesidePeek();
    expect(currentRoom().cover).toBe(false);
    expect(currentRoom().frameAfter).toBeGreaterThanOrEqual(PEEK_COVER_FLOOR);
  });

  describe("…on a window too small for the default split (Fix batch 6 §1)", () => {
    // Three branches, and the control has to be honest about all three: spend
    // the widest non-cover width, buy one with the sidebar, or say it cannot.
    const env = (viewport: number, baseline: number | null, collapsed = false) => ({
      viewport,
      baseline,
      sidebarExpanded: 232,
      sidebarCollapsed: collapsed,
    });

    it("falls back to the WIDEST non-cover width when the default is itself cover", () => {
      // 1132 less a 232 sidebar is 900 of content, against a baseline measured
      // at 300 — the default (content − baseline = 600) would leave the middle
      // pane 300px, under its 360 cover floor, so the panel is held back to
      // exactly what clears it.
      const plan = planShowList(env(1132, 300));
      expect(plan.collapse).toBe(false);
      expect(plan.width).toBe(900 - PEEK_COVER_FLOOR);
    });

    it("spends the sidebar when that is the only thing that clears it", () => {
      // 900 wide: 668 of content cannot hold a 400 panel and a 360 pane at
      // once, and 856 (the rail) can. The same trade the open-time exception
      // makes, and it is made here for the same 188px.
      const plan = planShowList(env(900, BASELINE));
      expect(plan.collapse).toBe(true);
      expect(plan.width).toBe(PEEK_MIN_WIDTH);
      expect(900 - SIDEBAR_RAIL - (plan.width ?? 0)).toBeGreaterThanOrEqual(PEEK_COVER_FLOOR);
    });

    it("answers null when not even the rail buys enough room", () => {
      expect(planShowList(env(700, BASELINE))).toEqual({ width: null, collapse: false });
      // …and a sidebar the reader has already collapsed has nothing left to
      // give, so the middle branch is not tried twice.
      expect(planShowList(env(900, BASELINE, true)).collapse).toBe(false);
    });

    it("collapses the sidebar for real, silently, and marks it ours", () => {
      windowWidth(900);
      setPeekHost(true);
      setPeekBaselineCandidate(BASELINE);
      openPeek("sess-1");
      // The open-time exception does not fire here (the collapse would not buy
      // the panel its minimum beside an untouched column), so the sidebar is
      // still open and the panel is covering.
      expect(sidebar.getSidebarState().collapsed).toBe(false);
      expect(currentRoom().cover).toBe(true);
      expect(canShowList()).toBe(true);
      showListBesidePeek();
      expect(sidebar.getSidebarState().collapsed).toBe(true);
      expect(currentRoom().cover).toBe(false);
      expect(currentRoom().frameAfter).toBeGreaterThanOrEqual(PEEK_COVER_FLOOR);
      // Ours to hand back when the reader leaves /tasks, and NOT a preference:
      // the layout got out of the way, the reader did not ask for a rail.
      expect(getPeekState().autoCollapsed).toBe(true);
      expect(localStorage.getItem("fused-render:sidebar")).toBeNull();
    });

    it("is refused, and says so, when the window cannot hold both", () => {
      windowWidth(700);
      setPeekHost(true);
      setPeekBaselineCandidate(BASELINE);
      openPeek("sess-1");
      expect(canShowList()).toBe(false);
      const before = getPeekState().width;
      showListBesidePeek();
      // Nothing moved — which is why the header draws the control disabled with
      // "Window too narrow to show the list" rather than offering the press.
      expect(getPeekState().width).toBe(before);
      expect(sidebar.getSidebarState().collapsed).toBe(false);
    });

    it("has nothing to offer with no panel open", () => {
      windowWidth(VIEWPORT);
      setPeekHost(true);
      expect(canShowList()).toBe(false);
    });
  });

  it("does nothing at all with no peek open", () => {
    // The header that offers it only exists while the panel does, but the store
    // is a module anyone can reach and "restore the split" is meaningless with
    // nothing to split.
    windowWidth(VIEWPORT);
    setPeekHost(true);
    const before = getPeekState();
    showListBesidePeek();
    expect(getPeekState()).toBe(before);
    expect(localStorage.getItem(PEEK_WIDTH_KEY)).toBeNull();
  });

  it("carries a MESSAGE anchor, and only from the press that named one", () => {
    // A message row addresses one turn of a conversation, which is smaller than
    // a task and is the one thing this page can address that a task row cannot
    // (design.md, Polish batch 3).
    arm();
    expect(getPeekState().anchor).toBeNull();
    openPeek("sess-2", { anchor: "turn-7" });
    expect(getPeekState().anchor).toBe("turn-7");
    // A plain open of another task means the thread, not a turn of it.
    openPeek("sess-3");
    expect(getPeekState().anchor).toBeNull();
  });

  it("re-anchors a conversation that is ALREADY open", () => {
    // Pressing a second message row in the same expanded thread swaps nothing
    // but the anchor — and an ordinary re-press of the open task still does
    // nothing at all.
    arm();
    openPeek("sess-2", { anchor: "turn-1" });
    expect(openPeek("sess-2", { anchor: "turn-9" })).toBe(true);
    expect(getPeekState().anchor).toBe("turn-9");
    expect(openPeek("sess-2")).toBe(true);
    expect(getPeekState().anchor).toBe("turn-9");
  });

  it("re-pressing the SAME message row changes nothing", () => {
    // Deliberately not a re-scroll: the turn is already where the reader put
    // it, and a press that silently jumps the transcript back to a place it is
    // already at reads as the panel losing their scroll position. Nobody has
    // asked for the other behaviour; if they do, this is the test to change.
    arm();
    openPeek("sess-2", { anchor: "turn-7" });
    const settled = getPeekState();
    expect(openPeek("sess-2", { anchor: "turn-7" })).toBe(true);
    expect(getPeekState()).toBe(settled);
  });

  it("forgets the anchor when the panel closes", () => {
    arm();
    openPeek("sess-2", { anchor: "turn-7" });
    closePeek();
    expect(getPeekState().anchor).toBeNull();
  });

  it("freezes the baseline at the FIRST open and keeps it across a swap", () => {
    arm();
    // The page goes on measuring — a window resize, a filter that changes the
    // column — and none of it moves the number the panel opened against.
    setPeekBaselineCandidate(700);
    openPeek("sess-2");
    expect(getPeekState().baseline).toBe(BASELINE);
  });
});

// ---- measureTasksBaseline: the gutter, tight or not (Bugbot, PR #1141) -----
// No jsdom in this suite (`fit.test.ts` sets the precedent), so the `.tasks-
// frame` section `measureTasksBaseline` reads off the real DOM is three plain
// objects here — `document.querySelector` answers its three exact selectors,
// and `getComputedStyle` answers each one's box. `--tasks-page-gutter` is
// faked exactly as `styles/schedule.css` declares it on `.schedule-page`:
// present and worth 44 whether or not `tightEachSide` (what `data-tight`,
// `styles/task-peek.css`, would have narrowed the LIVE padding to) says
// something smaller. `gutterVar: null` fakes a page that predates the var, to
// prove the padding-sum fallback still works.
function withFakeTasksDom(
  opts: { content: number; cap?: number; gutterVar?: number | null; tightEachSide?: number },
  fn: () => void,
): void {
  const cap = opts.cap ?? 1050;
  const untightEachSide = 22; // `.prefs-page`'s own padding (styles/preferences.css)
  const liveEachSide = opts.tightEachSide ?? untightEachSide;
  const main = { getBoundingClientRect: () => ({ width: Math.min(cap, opts.content) }) };
  const page = {};
  const host = { clientWidth: opts.content };
  const styles = new Map<unknown, Record<string, unknown>>();
  styles.set(main, {
    getPropertyValue: () => "",
    paddingLeft: "0px",
    paddingRight: "0px",
    maxWidth: `${cap}px`,
  });
  styles.set(page, {
    getPropertyValue: (name: string) =>
      name === "--tasks-page-gutter" && opts.gutterVar != null ? `${opts.gutterVar}px` : "",
    // The LIVE padding — 0 either side once `data-tight="1"` lands
    // (styles/task-peek.css), 22px either side otherwise. This is exactly
    // what the old, buggy read used, and every test below that passes a var
    // proves the fix no longer reads it.
    paddingLeft: `${liveEachSide}px`,
    paddingRight: `${liveEachSide}px`,
    maxWidth: "none",
  });

  interface FakeDoc {
    querySelector: (sel: string) => unknown;
  }
  const doc = globalThis as unknown as { document: FakeDoc };
  const realQuerySelector = doc.document.querySelector;
  const globalWithStyle = globalThis as { getComputedStyle?: (el: unknown) => unknown };
  const realGetComputedStyle = globalWithStyle.getComputedStyle;
  doc.document.querySelector = (sel: string) => {
    if (sel === ".tasks-frame .schedule-main") return main;
    if (sel === ".tasks-frame .schedule-page") return page;
    if (sel === ".tasks-peek-host") return host;
    return null;
  };
  globalWithStyle.getComputedStyle = (el: unknown) => styles.get(el) ?? {};
  try {
    fn();
  } finally {
    doc.document.querySelector = realQuerySelector;
    if (realGetComputedStyle === undefined) delete globalWithStyle.getComputedStyle;
    else globalWithStyle.getComputedStyle = realGetComputedStyle;
  }
}

describe("measureTasksBaseline — the gutter stays untight (Bugbot, PR #1141)", () => {
  it("measures the same gutter whether the frame is tight or not", () => {
    // Untight: the live padding (22+22) and the var (44) agree, as they would
    // on a wide frame that has never gone tight.
    withFakeTasksDom({ content: 1200, gutterVar: 44, tightEachSide: 22 }, () => {
      expect(measureTasksBaseline()).toBe(1094); // 1050 (cap) + 44 (gutter)
      expect(peekGutter()).toBe(44);
    });
    // Tight: the live padding is down to 0+0, but the var — which
    // `[data-tight="1"] .schedule-page` never redeclares — still says 44, and
    // that is the number the measurement takes. The bug read the padding sum
    // instead and would have answered 1050 (1050 + 0) here.
    withFakeTasksDom({ content: 1200, gutterVar: 44, tightEachSide: 0 }, () => {
      expect(measureTasksBaseline()).toBe(1094);
      expect(peekGutter()).toBe(44);
    });
  });

  it("a re-measure while tight, after the baseline is frozen, leaves peekGutter() unchanged", () => {
    windowWidth(1600);
    // Freeze the baseline untight, exactly as a normal first open would.
    withFakeTasksDom({ content: 1200, gutterVar: 44, tightEachSide: 22 }, () => {
      refreshPeekBaseline();
    });
    setPeekHost(true);
    openPeek("sess-1");
    expect(getPeekState().baseline).toBe(1094);
    expect(peekGutter()).toBe(44);
    // A resize lands the frame in tight AFTER the freeze — the observer goes
    // on running while the peek is open (`refreshPeekBaseline` is what
    // Scheduled.tsx's own ResizeObserver calls). Before the fix this
    // overwrote the module's `baselineGutter` with 0 on every such tick,
    // which is exactly what fed the wrong number into `contentFloor`
    // (Scheduled.tsx) for the rest of the visit.
    withFakeTasksDom({ content: 900, gutterVar: 44, tightEachSide: 0 }, () => {
      refreshPeekBaseline();
    });
    expect(getPeekState().baseline).toBe(1094); // already frozen, untouched
    expect(peekGutter()).toBe(44); // and neither is the gutter it was frozen with
  });

  it("a deep link that opens already tight still freezes the full gutter", () => {
    // The deep-link race (design.md, Widths v2 — "the store reads the DOM
    // itself"): `useTaskPeekHost` adopts `?peek=` in a layout effect, before
    // any passive ResizeObserver has run, so the FIRST measurement — landing
    // with the frame already narrow enough to be tight, live padding 0
    // either side — is what the visit's baseline (and its gutter) freezes
    // from. `state.baseline` is null and `baselineCandidate` is null too (no
    // observer tick has happened yet), so `syncPeekFromUrl` reaches all the
    // way to `measureTasksBaseline` itself for its very first reading.
    windowWidth(1600);
    setPeekHost(true);
    withFakeTasksDom({ content: 1200, gutterVar: 44, tightEachSide: 0 }, () => {
      syncPeekFromUrl("?peek=sess-1");
    });
    expect(getPeekState().key).toBe("sess-1");
    // 1050 (cap) + 44 (the var's gutter), not + 0 (the live tight padding
    // the old code read instead).
    expect(getPeekState().baseline).toBe(1094);
    expect(peekGutter()).toBe(44);
  });

  it("falls back to the padding sum on a page that predates the var", () => {
    withFakeTasksDom({ content: 1200, gutterVar: null, tightEachSide: 22 }, () => {
      expect(measureTasksBaseline()).toBe(1094); // 1050 + (22+22)
      expect(peekGutter()).toBe(44);
    });
  });
});

describe("the persisted keys", () => {
  it("are the two names the store reads and writes", () => {
    // Spelled here so a rename has to come through this file: the width is a
    // preference the reader built by dragging, and the marker is what stops a
    // reload stranding their sidebar.
    expect(PEEK_WIDTH_KEY).toBe("tasks.peek.width");
    expect(PEEK_AUTOCOLLAPSE_KEY).toBe("tasks.peek.autocollapsed");
  });
});

// ---- clicking the frame ------------------------------------------------------
// A hand-rolled node with a REAL `closest`: the rule under test is a selector
// list, and a stub that answered yes/no would be testing the stub. Only the
// selector shapes the list actually uses are understood (tag, .class,
// [attr], [attr="value"]) — a fifth shape appearing in `PEEK_FRAME_KEEPS_OPEN`
// should make this throw rather than quietly pass.
interface FakeNode {
  tag: string;
  classes?: string[];
  attrs?: Record<string, string>;
  parent?: FakeNode;
}

function matchesOne(node: FakeNode, selector: string): boolean {
  const sel = selector.trim();
  if (sel.startsWith(".")) return (node.classes ?? []).includes(sel.slice(1));
  if (sel.startsWith("[")) {
    const body = sel.slice(1, -1);
    const eq = body.indexOf("=");
    if (eq === -1) return body in (node.attrs ?? {});
    const name = body.slice(0, eq);
    const want = body.slice(eq + 1).replace(/^["']|["']$/g, "");
    return (node.attrs ?? {})[name] === want;
  }
  if (/^[a-z]+$/.test(sel)) return node.tag === sel;
  throw new Error(`unhandled selector shape: ${sel}`);
}

function el(node: FakeNode): Element {
  const self = {
    closest(list: string): Element | null {
      const parts = list.split(",");
      let at: FakeNode | undefined = node;
      while (at) {
        for (const part of parts) if (matchesOne(at, part)) return self;
        at = at.parent;
      }
      return null;
    },
  } as unknown as Element;
  return self;
}

describe("frameClickCloses", () => {
  const page: FakeNode = { tag: "div", classes: ["schedule-page"] };

  it("closes on the page's own background", () => {
    expect(frameClickCloses(el({ tag: "div", parent: page }))).toBe(true);
  });

  it("does NOT close on a row, a card or a chip — those open their own task", () => {
    const row: FakeNode = {
      tag: "div",
      classes: ["tasks-row"],
      attrs: { "data-peek-key": "sess-1" },
      parent: page,
    };
    // …including a press on the ink INSIDE one.
    expect(frameClickCloses(el({ tag: "span", parent: row }))).toBe(false);
    expect(frameClickCloses(el(row))).toBe(false);
  });

  it("does NOT close on the toolbar, or on a control anywhere", () => {
    const toolbar: FakeNode = { tag: "div", classes: ["schedule-toolbar"], parent: page };
    expect(frameClickCloses(el({ tag: "span", parent: toolbar }))).toBe(false);
    expect(frameClickCloses(el({ tag: "button", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "a", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "input", parent: page }))).toBe(false);
    expect(frameClickCloses(el({ tag: "div", attrs: { role: "button" }, parent: page }))).toBe(
      false,
    );
  });

  it("does NOT close on a menu or a dialog portalled over the page", () => {
    const menu: FakeNode = { tag: "div", classes: ["context-menu"] };
    expect(frameClickCloses(el({ tag: "div", parent: menu }))).toBe(false);
    const dialog: FakeNode = { tag: "div", classes: ["modal-dialog"] };
    expect(frameClickCloses(el({ tag: "p", parent: dialog }))).toBe(false);
  });

  it("says nothing about a click with no target at all", () => {
    expect(frameClickCloses(null)).toBe(false);
  });
});
