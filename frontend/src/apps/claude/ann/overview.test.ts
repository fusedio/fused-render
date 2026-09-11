// The send-time overview's two decisions: where each badge goes, and — when it
// cannot go anywhere — the sentence the wire prints verbatim.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

import { describe, expect, test } from "bun:test";

const { applyOverview, badgesFor } = await import("./overview");
const { ANN_OFFSCREEN_DETACHED, ANN_OFFSCREEN_SCROLLED } = await import("./types");
import type { Annotation } from "./types";

/** Just enough of an element for `rectOf` / `contentBox`: no intrinsic size, so
 *  the content box IS the rect and `getComputedStyle` is never reached. */
function elAt(left: number, top: number, width: number, height: number) {
  return {
    tagName: "DIV",
    getBoundingClientRect: () => ({ left, top, width, height }),
    ownerDocument: null,
  } as unknown as Element;
}

/** A PIXEL SURFACE: `iu`/`iv` are fractions of a picture, so only an element
 *  with an intrinsic size may be read through them (T:10160's
 *  `annIntrinsic(el)` guard). `object-fit` is unread here — no `ownerDocument`,
 *  so `contentBox` falls back to "fill", i.e. the rect itself. */
function imgAt(left: number, top: number, width: number, height: number) {
  return {
    tagName: "IMG",
    naturalWidth: width,
    naturalHeight: height,
    getBoundingClientRect: () => ({ left, top, width, height }),
    ownerDocument: null,
  } as unknown as Element;
}

const note = (over: Partial<Annotation>): Annotation => ({
  id: over.id ?? "n",
  content: "words",
  createdAt: 1,
  label: "A",
  ...over,
});

const stage = { clientWidth: 400, clientHeight: 300 };
const doc = { defaultView: { scrollX: 0, scrollY: 0 } } as unknown as Document;

describe("badge points (T:10145-10171)", () => {
  test("an element note is badged at its CENTRE, and marked done", () => {
    const c = note({ anchorId: "x" });
    const { badges, marks } = badgesFor([c], {
      doc,
      xo: false,
      stage,
      resolve: () => elAt(100, 50, 40, 20),
    });
    expect(badges).toEqual([{ x: 120, y: 60, label: "A" }]);
    expect(marks.n).toBe(true);
  });

  test("iu/iv name the pixel, not the box centre — on a picture", () => {
    const c = note({ anchorId: "x", iu: 0, iv: 1 });
    const { badges } = badgesFor([c], {
      doc,
      xo: false,
      stage,
      resolve: () => imgAt(100, 50, 40, 20),
    });
    expect(badges).toEqual([{ x: 100, y: 70, label: "A" }]);
  });

  test("…and are IGNORED on a plain element, where a fraction moves with the reflow", () => {
    const c = note({ anchorId: "x", iu: 0, iv: 1 });
    const { badges } = badgesFor([c], {
      doc,
      xo: false,
      stage,
      resolve: () => elAt(100, 50, 40, 20),
    });
    expect(badges).toEqual([{ x: 120, y: 60, label: "A" }]);
  });

  test("a point note is converted back through the CURRENT scroll", () => {
    const scrolled = { defaultView: { scrollX: 10, scrollY: 20 } } as unknown as Document;
    const c = note({ kind: "point", x: 60, y: 90 });
    const { badges } = badgesFor([c], { doc: scrolled, xo: false, stage, resolve: () => null });
    expect(badges).toEqual([{ x: 50, y: 70, label: "A" }]);
  });

  test("the cross-origin overlay uses the zero-scroll stand-in", () => {
    const c = note({ kind: "point", x: 60, y: 90 });
    const { badges } = badgesFor([c], { doc: null, xo: true, stage, resolve: () => null });
    expect(badges).toEqual([{ x: 60, y: 90, label: "A" }]);
  });

  test("a missing label is still a badge — `?` beats a blank disc", () => {
    const c = note({ kind: "point", x: 1, y: 1, label: undefined });
    const { badges } = badgesFor([c], { doc, xo: false, stage, resolve: () => null });
    expect(badges[0].label).toBe("?");
  });
});

describe("the two offscreen reasons, verbatim (T:10166, 10173)", () => {
  test("an element the app has re-rendered away", () => {
    const c = note({ anchorPath: "div:nth-of-type(1)" });
    const { badges, marks } = badgesFor([c], {
      doc,
      xo: false,
      stage,
      resolve: () => null,
    });
    expect(badges).toEqual([]);
    expect(marks.n).toBe(ANN_OFFSCREEN_DETACHED);
    expect(marks.n).toBe(
      "the annotated element was not in the app's DOM when this " +
        "message was sent (the app may have re-rendered since)",
    );
  });

  test("a spot scrolled out of the visible pane, on every edge", () => {
    const rows: Array<[string, Annotation]> = [
      ["left", note({ id: "l", kind: "point", x: -1, y: 10 })],
      ["top", note({ id: "t", kind: "point", x: 10, y: -1 })],
      ["right", note({ id: "r", kind: "point", x: 401, y: 10 })],
      ["bottom", note({ id: "b", kind: "point", x: 10, y: 301 })],
    ];
    const { badges, marks } = badgesFor(
      rows.map(([, c]) => c),
      { doc, xo: false, stage, resolve: () => null },
    );
    expect(badges).toEqual([]);
    for (const [, c] of rows) expect(marks[c.id]).toBe(ANN_OFFSCREEN_SCROLLED);
    expect(marks.l).toBe(
      "the spot was scrolled out of the visible pane when this message was sent",
    );
  });

  test("a point with no window to convert through is offscreen, not a crash", () => {
    const c = note({ kind: "point", x: 10, y: 10 });
    const { marks } = badgesFor([c], { doc: null, xo: false, stage, resolve: () => null });
    expect(marks.n).toBe(ANN_OFFSCREEN_SCROLLED);
  });

  test("an element note with no document at all reads as detached", () => {
    const c = note({ anchorId: "x" });
    const { marks } = badgesFor([c], { doc: null, xo: false, stage, resolve: () => null });
    expect(marks.n).toBe(ANN_OFFSCREEN_DETACHED);
  });

  test("the edges are INCLUSIVE — a badge exactly on the boundary still lands", () => {
    const c = note({ kind: "point", x: 400, y: 300 });
    const { badges } = badgesFor([c], { doc, xo: false, stage, resolve: () => null });
    expect(badges).toHaveLength(1);
  });
});

describe("applyOverview clears stale fields FIRST (T:10256)", () => {
  test("a previous send's leftovers go, whatever this send says", () => {
    const c = note({
      offscreen: "an older send's reason",
      shot: null,
      shotNote: "a crop from the old wire format",
    });
    const [out] = applyOverview([c], { n: true });
    expect(out.offscreen).toBeUndefined();
    expect("shot" in out).toBe(false);
    expect("shotNote" in out).toBe(false);
  });

  test("…and this send's reason is written in their place", () => {
    const c = note({ offscreen: "an older send's reason" });
    const [out] = applyOverview([c], { n: ANN_OFFSCREEN_SCROLLED });
    expect(out.offscreen).toBe(ANN_OFFSCREEN_SCROLLED);
  });

  test("no marks at all (an abandoned capture) still clears", () => {
    const c = note({ offscreen: "stale" });
    expect(applyOverview([c], null)[0].offscreen).toBeUndefined();
  });

  test("the input notes are not mutated — the store owns the write", () => {
    const c = note({ offscreen: "stale" });
    applyOverview([c], { n: true });
    expect(c.offscreen).toBe("stale");
  });
});
