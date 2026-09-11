// THE STRIP'S VERDICT, PROVEN WITHOUT A LAYOUT ENGINE (inventory 03 §C).
//
// `fitStrip(row, need = measureRowNeed)` carries that second parameter for one
// reason: the decision it makes is arithmetic — what the content needs against
// what the box has — and the only thing standing between a test and that
// arithmetic is a browser's layout. So `need` is injected here and every branch
// of the verdict is driven directly.
//
// What is under test is the DECISION and the invariant around it, not the
// classList plumbing: the measurement happens with the words ON (`.tight` off,
// or the row measures the folded width and never unfolds), an unmeasurable row
// gets no verdict at all (the first-frame flash), and re-seating the observers
// on the same row leaves one set behind and not two.
//
// P3R1-1 adds the two properties the owner's divider drag was missing, and they
// are the ones a width SEQUENCE proves rather than a single call: the natural
// width is measured once per content generation (so a drag writes nothing per
// frame — the flicker), and `.tight` is never on while the words fit (so the
// strip cannot get stuck folded with room to spare). The hysteresis is spent on
// the fold and never on the return, which is what makes those two compatible.
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { createStripFit, FIT_HYSTERESIS, fitStrip, useFitStrip } = await import(
  "./useFitStrip"
);

// ── the fake row ────────────────────────────────────────────────────────────
// Only the four members `fitStrip` and the observer seating touch: the class
// list it writes, the width it reads, and the children the child observer sits
// on.

class Row {
  classes = new Set<string>();
  clientWidth = 0;
  kids: unknown[] = [];
  /** The `.chat-root` ancestor the third observer sits on, or null for a row
   *  that is not in a chat at all (the hook must seat nothing then). */
  root: unknown = { tag: "chat-root" };
  classList = {
    add: (c: string): void => void this.classes.add(c),
    remove: (c: string): void => void this.classes.delete(c),
    contains: (c: string): boolean => this.classes.has(c),
  };
  closest(sel: string): unknown {
    return sel === ".chat-root" ? this.root : null;
  }
  get children(): unknown[] {
    return this.kids;
  }
  get tight(): boolean {
    return this.classes.has("tight");
  }
}

const el = (r: Row): HTMLElement => r as unknown as HTMLElement;

/** A `need` that answers `px` and records what the row looked like WHILE it was
 *  being asked — the one thing a caller cannot observe afterwards, because
 *  `fitStrip` puts the class back before it returns. */
function needing(px: number): { need: (row: HTMLElement) => number; calls: boolean[] } {
  const calls: boolean[] = [];
  return {
    need: (row) => {
      calls.push((row as unknown as Row).tight);
      return px;
    },
    calls,
  };
}

describe("fitStrip — the verdict (T:7566)", () => {
  test("what the content needs does not fit: `.tight` goes on", () => {
    const row = new Row();
    row.clientWidth = 300;
    fitStrip(el(row), () => 308.3);
    expect(row.tight).toBe(true);
  });

  test("it fits: `.tight` comes back off, so widening undoes itself", () => {
    const row = new Row();
    row.clientWidth = 420;
    // The state a narrow layout left behind — the whole point of recomputing
    // from scratch rather than keeping a flag.
    row.classes.add("tight");
    fitStrip(el(row), () => 308.3);
    expect(row.tight).toBe(false);
  });

  test("QA #2's OWN PAIR — 308.3px of words in a 308px row (R1-6)", () => {
    // The example the file header is written around, and the one the hysteresis
    // changed the answer to: over by 0.3px, which the 3px band absorbs. Legacy
    // folds here and native does not, deliberately — a strip sitting a fraction
    // inside its own boundary flipping on every jitter of a divider drag is the
    // fault the band was added for, and 0.3px of overflow is paid by the ⋮'s own
    // 16px of row padding. The case was edited out (308 → 300) rather than
    // re-pinned when the band landed; this is it back, asserting the NEW answer
    // so the divergence is recorded instead of lost.
    const row = new Row();
    row.clientWidth = 308;
    fitStrip(el(row), () => 308.3);
    expect(row.tight).toBe(false);
    // A row that arrives FOLDED at that width stays folded, and that is the
    // one-way band being honest rather than inconsistent: 308.3 genuinely does
    // not fit in 308, so `.tight` is not outliving any overflow. It comes off
    // at the first width that fits the words outright.
    const back = new Row();
    back.clientWidth = 308;
    back.classes.add("tight");
    fitStrip(el(back), () => 308.3);
    expect(back.tight).toBe(true);
    back.clientWidth = 309;
    fitStrip(el(back), () => 308.3);
    expect(back.tight).toBe(false);
    // One more px of words and it is past the band, which is where legacy and
    // native agree again.
    const over = new Row();
    over.clientWidth = 308;
    fitStrip(el(over), () => 311.3);
    expect(over.tight).toBe(true);
  });

  test("exactly the width available FITS: the compare is `>` and not `>=`", () => {
    const row = new Row();
    row.clientWidth = 300;
    fitStrip(el(row), () => 300);
    expect(row.tight).toBe(false);
  });

  test("THE BAND IS SPENT ON THE FOLD, never on the return (P3R1-1)", () => {
    // Over by less than the hysteresis is not over enough to fold: this is the
    // 2px of headroom the live strip sits on (380px of row, a 378px need), and
    // it is what made the words flip on every jitter of a divider drag.
    const wobble = new Row();
    wobble.clientWidth = 300;
    fitStrip(el(wobble), () => 300 + FIT_HYSTERESIS);
    expect(wobble.tight).toBe(false);
    // Past it, it folds.
    const over = new Row();
    over.clientWidth = 300;
    fitStrip(el(over), () => 300 + FIT_HYSTERESIS + 0.5);
    expect(over.tight).toBe(true);
    // And a FOLDED row unfolds the moment the words fit AT ALL — the band buys
    // no delay on the way back, so `.tight` never outlives its overflow.
    const back = new Row();
    back.clientWidth = 300;
    back.classes.add("tight");
    fitStrip(el(back), () => 300);
    expect(back.tight).toBe(false);
  });

  test("MEASURED WITH THE WORDS ON — `need` is asked with `.tight` off", () => {
    const row = new Row();
    row.clientWidth = 100;
    row.classes.add("tight");
    const { need, calls } = needing(200);
    fitStrip(el(row), need);
    // Asked once, and asked of the row as the user would see it unfolded. Asking
    // a folded row what it needs measures the icons and the strip never unfolds
    // again.
    expect(calls).toEqual([false]);
    expect(row.tight).toBe(true);
  });

  test("A ROW WITH NO WIDTH GETS NO VERDICT — the first-frame `.tight` flash", () => {
    // The mount order: the ref callback runs during commit, `clientWidth` is
    // still 0, and any `need > 0` against that stamped `.tight` for one frame
    // before the ResizeObserver's first real delivery took it off — the strip
    // painted icon-only on every mount.
    const row = new Row();
    row.clientWidth = 0;
    const { need, calls } = needing(200);
    fitStrip(el(row), need);
    expect(calls).toEqual([]);
    expect(row.tight).toBe(false);
  });

  test("and it is left EXACTLY as it was: unmeasurable is not `it fits`", () => {
    // A strip already folded that is momentarily unmeasurable (a collapsed
    // parent, a display:none ancestor) must not unfold on the way back.
    const row = new Row();
    row.clientWidth = 0;
    row.classes.add("tight");
    const { need, calls } = needing(1);
    fitStrip(el(row), need);
    expect(calls).toEqual([]);
    expect(row.tight).toBe(true);
  });

  test("no row at all is a no-op, not a throw", () => {
    const { need, calls } = needing(200);
    expect(() => fitStrip(null, need)).not.toThrow();
    expect(() => fitStrip(undefined, need)).not.toThrow();
    expect(calls).toEqual([]);
  });
});

// ── the cached natural width, and the width sequence (P3R1-1) ───────────────

describe("createStripFit — the natural width is measured ONCE", () => {
  test("a whole drag costs ONE measurement, and writes nothing per frame", () => {
    // The bug this pins: the old decider took `.tight` off, measured, and put
    // it back on EVERY ResizeObserver delivery — a DOM write per frame of a
    // divider drag, which is what painted the labels in and out.
    const row = new Row();
    const { need, calls } = needing(320);
    const fit = createStripFit(need);
    row.clientWidth = 400;
    fit.run(el(row));
    expect(calls).toHaveLength(1);
    for (const w of [396, 390, 380, 360, 340, 330, 320, 300, 340, 400]) {
      row.clientWidth = w;
      fit.run(el(row));
    }
    // Still one: the natural width is a property of the CONTENT, so the box
    // moving is not a reason to ask again.
    expect(calls).toHaveLength(1);
    expect(fit.natural()).toBe(320);
  });

  test("wide → narrow → wide → jitter: the verdict is stable and correct", () => {
    const row = new Row();
    const fit = createStripFit(() => 320);
    const seen: [number, boolean][] = [];
    // 320 of content: the fold lands below 320 - FIT_HYSTERESIS, the unfold at
    // 320 exactly.
    const seq = [
      480, 420, 360, 330, 324, 323, 316, 300, 280, 300, 316, 320, 324, 360, 420, 480,
      // …and the jitter a real divider delivers, ±2px across the boundary.
      322, 318, 320, 322, 318, 320,
    ];
    for (const w of seq) {
      row.clientWidth = w;
      fit.run(el(row));
      seen.push([w, row.tight]);
    }
    // THE INVARIANT: never folded while the words fit.
    for (const [w, tight] of seen) if (tight) expect(w).toBeLessThan(320);
    // …and never left unfolded once it is clearly over.
    for (const [w, tight] of seen) if (w < 320 - FIT_HYSTERESIS) expect(tight).toBe(true);
    // The tail is the jitter, all of it at or above 320: the words are back and
    // they STAY back — no flip anywhere in it (the "stays collapsed even with
    // room" half of the report).
    expect(seen.slice(-6).map(([, t]) => t)).toEqual([false, false, false, false, false, false]);
    // One transition down and one back up across the whole sweep, and that is
    // all: 22 widths, two changes of mind.
    const flips = seen.filter(([, t], i) => i > 0 && t !== seen[i - 1][1]).length;
    expect(flips).toBe(2);
  });

  test("the content changing is what re-measures — nothing else does", () => {
    const row = new Row();
    let want = 320;
    const asked: number[] = [];
    const fit = createStripFit(() => {
      asked.push(want);
      return want;
    });
    row.clientWidth = 400;
    fit.run(el(row));
    expect(row.tight).toBe(false);
    // A label grows past the box. Without the invalidation the strip would go
    // on believing the old, narrower content.
    want = 460;
    fit.run(el(row));
    expect(row.tight).toBe(false);
    fit.invalidate();
    fit.run(el(row));
    expect(row.tight).toBe(true);
    expect(asked).toEqual([320, 460]);
  });

  test("the probe puts `.tight` BACK before measuring a folded row", () => {
    // The measurement is a probe, not a state change: a folded row is asked
    // what it needs unfolded and is handed back exactly as it was, so the
    // verdict below it is the only thing that can move the class.
    const row = new Row();
    row.clientWidth = 200;
    row.classes.add("tight");
    const fit = createStripFit((r) => ((r as unknown as Row).tight ? 120 : 320));
    fit.run(el(row));
    // Measured unfolded (320, not the folded 120) → still does not fit → stays.
    expect(fit.natural()).toBe(320);
    expect(row.tight).toBe(true);
  });
});

// ── the observers ───────────────────────────────────────────────────────────

const G = globalThis as Record<string, unknown>;
const BEFORE = { RO: G.ResizeObserver, MO: G.MutationObserver };
afterAll(() => {
  G.ResizeObserver = BEFORE.RO;
  G.MutationObserver = BEFORE.MO;
});

interface Seen {
  ro: { targets: unknown[]; disconnects: number; fire: () => void };
  mo: { observed: { target: unknown; opts: unknown; cb: () => void }[]; disconnects: number };
}

/** Both observer constructors, counted. `fire` is the ResizeObserver's delivery
 *  road — the one that hands over the first verdict that means anything. */
function observers(): Seen {
  const seen: Seen = {
    ro: { targets: [], disconnects: 0, fire: () => {} },
    mo: { observed: [], disconnects: 0 },
  };
  G.ResizeObserver = class {
    constructor(cb: () => void) {
      seen.ro.fire = cb;
    }
    observe(target: unknown): void {
      seen.ro.targets.push(target);
    }
    disconnect(): void {
      seen.ro.disconnects++;
    }
  };
  G.MutationObserver = class {
    cb: () => void;
    constructor(cb: () => void) {
      this.cb = cb;
    }
    observe(target: unknown, opts: unknown): void {
      // The CALLBACK is recorded with the target: which of the hook's two
      // callbacks a node was handed is the difference between a re-decide and a
      // cache invalidation (R1-6).
      seen.mo.observed.push({ target, opts, cb: this.cb });
    }
    disconnect(): void {
      seen.mo.disconnects++;
    }
  };
  return seen;
}

/** The hook hands out a ref CALLBACK, so a host is mounted to get at it — the
 *  teardown under test lives in the ref the hook keeps between calls. */
function mountHook(): (row: HTMLElement | null) => void {
  let seat!: (row: HTMLElement | null) => void;
  function Host(): null {
    seat = useFitStrip();
    return null;
  }
  act(() => {
    mounted.push(create(createElement(Host)));
  });
  return seat;
}

const mounted: ReturnType<typeof create>[] = [];
let seen: Seen;
beforeEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  seen = observers();
});

describe("useFitStrip — the wiring", () => {
  test("the row gets a ResizeObserver, its CHILDREN get the mutation one", () => {
    const row = new Row();
    row.kids = [{ tag: "a" }, { tag: "b" }];
    row.clientWidth = 0;
    mountHook()(el(row));
    expect(seen.ro.targets).toEqual([row]);
    // Two child observers plus the childList-only one on the row itself, which
    // is what re-seats them when React adds or removes a seat.
    const onRow = seen.mo.observed.filter((o) => o.target === row);
    expect(onRow).toHaveLength(1);
    expect(onRow[0]!.opts).toEqual({ childList: true });
    expect(
      seen.mo.observed
        .filter((o) => o.target !== row && o.target !== row.root)
        .map((o) => o.target),
    ).toEqual(row.kids);
  });

  test("…and the `.chat-root` ANCESTOR gets one for its class list (R1-6)", () => {
    // The row's natural width depends on CSS state outside the row:
    // `.chat-root.narrow.view-preview .c-back { display: none }` drops a seat
    // out of the measurement entirely, so a generation first measured there is
    // ~78px short and stays short. Neither of the other two observers can see
    // that flip — the class belongs to an ancestor — and the rescue was
    // incidental (the seats that mount on the same flip happen to produce a
    // child record).
    const row = new Row();
    row.kids = [{ tag: "a" }];
    row.clientWidth = 0;
    mountHook()(el(row));
    const onRoot = seen.mo.observed.filter((o) => o.target === row.root);
    expect(onRoot).toHaveLength(1);
    // CLASS ONLY, and no subtree: this is a question about one attribute on one
    // node, and the hook writes the ROW's class list — never the root's — so no
    // verdict it makes can schedule the next one.
    expect(onRoot[0]!.opts).toEqual({ attributes: true, attributeFilter: ["class"] });
  });

  test("a row with no `.chat-root` above it seats no ancestor observer (R1-6)", () => {
    // `closest` answers null in the flag-off shells and in any test that mounts
    // the strip on its own; the hook must simply skip the third observer rather
    // than observe `null`.
    const row = new Row();
    row.root = null;
    row.kids = [{ tag: "a" }];
    expect(() => mountHook()(el(row))).not.toThrow();
    expect(seen.mo.observed.filter((o) => o.target === null)).toHaveLength(0);
    // Still exactly the two it always had: the row's childList and its one kid.
    expect(seen.mo.observed).toHaveLength(2);
  });

  test("the ROOT observer's callback IS the invalidating one (R1-6)", () => {
    // The property, not just the wiring: a root class flip has to throw the
    // cached natural width away, not merely re-run the comparison against it —
    // the whole fault is a cache taken while a seat was hidden. `recheck` is the
    // invalidating callback (the children share it) and `run` is the plain
    // re-decide the ResizeObserver gets, so the proof is which of the two the
    // root was handed.
    const row = new Row();
    row.kids = [{ tag: "a" }];
    row.clientWidth = 0;
    mountHook()(el(row));
    const cbFor = (target: unknown) => seen.mo.observed.find((o) => o.target === target)?.cb;
    expect(cbFor(row.root)).toBe(cbFor(row.kids[0]));
    // …and NOT the row's own childList callback, which re-seats the child
    // observers before rechecking — the root gains and loses no children here.
    expect(cbFor(row.root)).not.toBe(cbFor(row));
  });

  test("re-seating the SAME row leaves one set of observers behind, not two", () => {
    // React calls a ref callback again whenever the callback's identity or the
    // node changes; without the teardown every re-seat doubled the deliveries
    // and each verdict scheduled the next.
    const row = new Row();
    row.kids = [{ tag: "a" }];
    const seat = mountHook();
    seat(el(row));
    const first = seen.ro.targets.length;
    seat(el(row));
    // The previous set is disconnected before the new one is wired: one
    // ResizeObserver and both MutationObservers.
    expect(seen.ro.disconnects).toBe(1);
    expect(seen.ro.targets).toHaveLength(first + 1);
    // And the row on the way OUT takes everything with it.
    seat(null);
    expect(seen.ro.disconnects).toBe(2);
  });

  test("the ref callback's first run does NOT measure a just-committed row", () => {
    // No injected `need` here on purpose: the default is `measureRowNeed`, which
    // reads `getComputedStyle` — so if the mount verdict ran at all against this
    // fake it would either throw or stamp `.tight`. It does neither, which is
    // the guard.
    const row = new Row();
    row.clientWidth = 0;
    expect(() => mountHook()(el(row))).not.toThrow();
    expect(row.tight).toBe(false);
    // Once the box is real, the observer's delivery is the first verdict.
    row.clientWidth = 10;
    fitStrip(el(row), () => 99);
    expect(row.tight).toBe(true);
  });
});
