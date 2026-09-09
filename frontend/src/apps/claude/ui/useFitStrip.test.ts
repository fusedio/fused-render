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
import { afterAll, beforeEach, describe, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { createElement } from "react";
import { act, create } from "react-test-renderer";

const { fitStrip, useFitStrip } = await import("./useFitStrip");

// ── the fake row ────────────────────────────────────────────────────────────
// Only the four members `fitStrip` and the observer seating touch: the class
// list it writes, the width it reads, and the children the child observer sits
// on.

class Row {
  classes = new Set<string>();
  clientWidth = 0;
  kids: unknown[] = [];
  classList = {
    add: (c: string): void => void this.classes.add(c),
    remove: (c: string): void => void this.classes.delete(c),
    contains: (c: string): boolean => this.classes.has(c),
  };
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
    row.clientWidth = 308;
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

  test("exactly the width available FITS: the compare is `>` and not `>=`", () => {
    const row = new Row();
    row.clientWidth = 300;
    fitStrip(el(row), () => 300);
    expect(row.tight).toBe(false);
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

// ── the observers ───────────────────────────────────────────────────────────

const G = globalThis as Record<string, unknown>;
const BEFORE = { RO: G.ResizeObserver, MO: G.MutationObserver };
afterAll(() => {
  G.ResizeObserver = BEFORE.RO;
  G.MutationObserver = BEFORE.MO;
});

interface Seen {
  ro: { targets: unknown[]; disconnects: number; fire: () => void };
  mo: { observed: { target: unknown; opts: unknown }[]; disconnects: number };
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
    observe(target: unknown, opts: unknown): void {
      seen.mo.observed.push({ target, opts });
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
    expect(seen.mo.observed.filter((o) => o.target !== row).map((o) => o.target)).toEqual(row.kids);
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
