// §G's formulas, asserted against the numbers in the inventory. Pure arithmetic,
// so no DOM shim is needed for most of it — the two DOM-reading helpers
// (`contentBox`, `pathOf`, `resolveIn`) get hand-built stubs.
import { describe, expect, test } from "bun:test";

import {
  ANN_PIN_CLAMP,
  ANN_POP_H,
  ANN_POP_W,
  badgeXY,
  barFolds,
  barNeed,
  chipEditXY,
  clockOf,
  contentBoxOf,
  elementPinXY,
  iuivAt,
  labelFor,
  pageXY,
  pathOf,
  pinAt,
  pointXY,
  popAt,
  resolveIn,
  stampOf,
} from "./geometry";

describe("content box — object-fit (T:6701-6711)", () => {
  const r = { left: 100, top: 50, width: 400, height: 200 };
  const nat = { w: 100, h: 100 };

  test("contain letterboxes and centres", () => {
    // s = min(400/100, 200/100) = 2 → 200x200, centred in a 400x200 box
    expect(contentBoxOf(r, nat, "contain")).toEqual({
      left: 200,
      top: 50,
      width: 200,
      height: 200,
    });
  });

  test("cover fills and overflows", () => {
    // s = max(4, 2) = 4 → 400x400, so the top is pushed 100 above the box
    expect(contentBoxOf(r, nat, "cover")).toEqual({
      left: 100,
      top: -50,
      width: 400,
      height: 400,
    });
  });

  test("scale-down never enlarges", () => {
    // contain would say 2; scale-down clamps to 1 → the natural 100x100
    expect(contentBoxOf(r, nat, "scale-down")).toEqual({
      left: 250,
      top: 100,
      width: 100,
      height: 100,
    });
  });
});

describe("iu/iv (T:8672)", () => {
  const b = { left: 10, top: 20, width: 200, height: 100 };
  test("three decimals, clamped to the box", () => {
    expect(iuivAt(110, 70, b)).toEqual({ iu: 0.5, iv: 0.5 });
    expect(iuivAt(-100, -100, b)).toEqual({ iu: 0, iv: 0 });
    expect(iuivAt(9999, 9999, b)).toEqual({ iu: 1, iv: 1 });
    // 33/200 = 0.165 exactly; 1/3 of the height rounds to three places
    expect(iuivAt(43, 20 + 100 / 3, b)).toEqual({ iu: 0.165, iv: 0.333 });
  });
  test("a zero-size box has no fractions to be", () => {
    expect(iuivAt(1, 1, { left: 0, top: 0, width: 0, height: 10 })).toBeNull();
  });
});

describe("page ↔ viewport (T:8631, 7949)", () => {
  test("stored as page coords, rounded", () => {
    expect(pageXY(10.4, 20.6, { scrollX: 5, scrollY: 100 })).toEqual({ x: 15, y: 121 });
  });
  test("read back through the CURRENT scroll", () => {
    expect(pointXY({ x: 15, y: 121 }, { scrollX: 0, scrollY: 0 })).toEqual({ x: 15, y: 121 });
    expect(pointXY({ x: 15, y: 121 }, { scrollX: 5, scrollY: 100 })).toEqual({ x: 10, y: 21 });
  });
  test("no window, no answer — the caller falls back", () => {
    expect(pointXY({ x: 1, y: 1 }, null)).toBeNull();
    expect(pointXY({}, { scrollX: 0 })).toBeNull();
  });
  test("a missing scroll axis counts as zero, not NaN", () => {
    expect(pageXY(10, 10, {})).toEqual({ x: 10, y: 10 });
  });
});

describe("pin clamp, 14px (T:6927)", () => {
  const host = { clientWidth: 300, clientHeight: 200 };
  test("held a half-pin off the left, right and top edges", () => {
    expect(pinAt(0, 0, host)).toEqual({ left: ANN_PIN_CLAMP, top: ANN_PIN_CLAMP });
    expect(pinAt(999, 100, host)).toEqual({ left: 300 - ANN_PIN_CLAMP, top: 100 });
    expect(pinAt(150, 120, host)).toEqual({ left: 150, top: 120 });
  });
  test("scrolled out of the framed viewport draws no pin", () => {
    expect(pinAt(150, -1, host)).toBeNull();
    expect(pinAt(150, 201, host)).toBeNull();
    expect(pinAt(-1, 100, host)).toBeNull();
  });
});

describe("popover clamp, 292/110 (T:7341)", () => {
  const host = { clientWidth: 400, clientHeight: 300 };
  test("ten down and right, then clamped", () => {
    expect(popAt(10, 10, host)).toEqual({ left: 20, top: 20 });
    expect(popAt(390, 290, host)).toEqual({ left: 400 - ANN_POP_W, top: 300 - ANN_POP_H });
    expect(popAt(-100, -100, host)).toEqual({ left: 0, top: 0 });
  });
});

describe("the spot a note marks (T:6923, 10163)", () => {
  const rect = { left: 10, top: 20, width: 100, height: 50 };
  const box = { left: 10, top: 20, width: 100, height: 50 };
  test("a plain element note is PINNED top-right and BADGED centre", () => {
    expect(elementPinXY({}, box, rect)).toEqual({ x: 110, y: 20 });
    expect(badgeXY({}, box, rect)).toEqual({ x: 60, y: 45 });
  });
  test("iu/iv win for both — the pixel, not the box", () => {
    expect(elementPinXY({ iu: 0.5, iv: 0.5 }, box, rect)).toEqual({ x: 60, y: 45 });
    expect(badgeXY({ iu: 0.25, iv: 0 }, box, rect)).toEqual({ x: 35, y: 20 });
  });
  test("a badge with no content box falls back to the rect's centre", () => {
    expect(badgeXY({ iu: 0.5, iv: 0.5 }, null, rect)).toEqual({ x: 60, y: 45 });
  });
});

describe("chip-edit coordinates (T:6990)", () => {
  const host = { clientWidth: 300, clientHeight: 180 };
  test("a point has its own coordinate", () => {
    expect(chipEditXY({ x: 7, y: 9 }, null, host, true)).toEqual({ x: 7, y: 9 });
  });
  test("an element note falls to its rect's top-right", () => {
    expect(
      chipEditXY(null, { left: 10, top: 20, width: 5, height: 5 }, host, false),
    ).toEqual({ x: 15, y: 20 });
  });
  test("unresolvable: thirds of the pane split, null hosted", () => {
    expect(chipEditXY(null, null, host, false)).toEqual({ x: 100, y: 60 });
    expect(chipEditXY(null, null, host, true)).toEqual({ x: null, y: null });
  });
});

describe("labels (T:6733)", () => {
  test("bijective base-26 — A, Z, AA", () => {
    expect(labelFor(0)).toBe("A");
    expect(labelFor(25)).toBe("Z");
    expect(labelFor(26)).toBe("AA");
    expect(labelFor(51)).toBe("AZ");
    expect(labelFor(52)).toBe("BA");
  });
});

describe("the bar's folds (T:6733-6757)", () => {
  const m = { tag: 60, slot: 100, discard: 30, done: 70, stop: 0 };
  test("need sums only the seats that are PRESENT, and their gaps", () => {
    // slot + discard + done are non-zero → 3 gaps
    expect(barNeed(m, 12)).toBe(60 + 200 + 36);
  });
  test("the sentence yields first, then the words, then the tag's word", () => {
    const wide = barNeed(m, 12); // 296
    expect(barFolds(wide + 200, 12, 100, m)).toEqual({ t1: false, t2: false, t3: false });
    // no room for the sentence's own natural width plus the 10px gutter
    expect(barFolds(wide + 50, 12, 100, m)).toEqual({ t1: true, t2: false, t3: false });
    // no room for the controls at all
    expect(barFolds(wide - 10, 12, 100, m)).toEqual({ t1: true, t2: true, t3: true });
    // …unless the icon-only re-measure fits, in which case the tag keeps its word
    const folded = { ...m, slot: 40, done: 30 };
    expect(barFolds(wide - 10, 12, 100, m, folded)).toEqual({ t1: true, t2: true, t3: false });
  });
});

describe("clocks and stamps", () => {
  test("m:ss (T:7815)", () => {
    expect(clockOf(0)).toBe("0:00");
    expect(clockOf(59.9)).toBe("0:59");
    expect(clockOf(61)).toBe("1:01");
    expect(clockOf(600)).toBe("10:00");
  });
  test("a stamp is seconds to a TENTH, not seventeen digits (T:7943)", () => {
    expect(stampOf(2415.0000000372529)).toBe(2.4);
    expect(stampOf(0)).toBe(0);
    expect(stampOf(10490)).toBe(10.5);
  });
});

// ── the two DOM readers, over a hand-built tree ────────────────────────────

interface FakeEl {
  tagName: string;
  id?: string;
  children: FakeEl[];
  parentElement: FakeEl | null;
  previousElementSibling: FakeEl | null;
  nodeType: number;
}

function el(tagName: string, kids: FakeEl[] = [], id?: string): FakeEl {
  const node: FakeEl = {
    tagName,
    id,
    children: kids,
    parentElement: null,
    previousElementSibling: null,
    nodeType: 1,
  };
  kids.forEach((k, i) => {
    k.parentElement = node;
    k.previousElementSibling = i ? kids[i - 1] : null;
  });
  return node;
}

function docOf(body: FakeEl): { body: FakeEl; getElementById(id: string): FakeEl | null } {
  const walk = (n: FakeEl, out: FakeEl[]): FakeEl[] => {
    out.push(n);
    for (const k of n.children) walk(k, out);
    return out;
  };
  const all = walk(body, []);
  return { body, getElementById: (id) => all.find((n) => n.id === id) ?? null };
}

describe("pathOf / resolveIn (T:6667, 6680)", () => {
  const p1 = el("P");
  const p2 = el("P");
  const span = el("SPAN", [], "hit");
  const div = el("DIV", [p1, p2, span]);
  const body = el("BODY", [div]);
  const doc = docOf(body);
  const asDoc = doc as unknown as Document;

  test("a tag:nth-of-type chain from body, nth counted per TAG", () => {
    expect(pathOf(p2 as unknown as Element, asDoc)).toBe("div:nth-of-type(1)>p:nth-of-type(2)");
    expect(pathOf(span as unknown as Element, asDoc)).toBe(
      "div:nth-of-type(1)>span:nth-of-type(1)",
    );
  });

  test("a node outside the body has no path", () => {
    expect(pathOf(el("P") as unknown as Element, asDoc)).toBeNull();
  });

  test("resolve walks it back", () => {
    expect(resolveIn({ anchorPath: "div:nth-of-type(1)>p:nth-of-type(2)" }, asDoc)).toBe(
      p2 as unknown as Element,
    );
  });

  test("the id wins over the path — it survives a re-render the path does not", () => {
    expect(resolveIn({ anchorId: "hit", anchorPath: "div:nth-of-type(9)" }, asDoc)).toBe(
      span as unknown as Element,
    );
  });

  test("case-INSENSITIVE, so an anchor inside an <svg> is not silently lost", () => {
    const g = el("g");
    const svg = el("svg", [g]);
    const b2 = el("BODY", [svg]);
    const d2 = docOf(b2) as unknown as Document;
    const path = pathOf(g as unknown as Element, d2);
    expect(path).toBe("svg:nth-of-type(1)>g:nth-of-type(1)");
    expect(resolveIn({ anchorPath: path as string }, d2)).toBe(g as unknown as Element);
  });

  test("a path that no longer resolves is null, never a wrong element", () => {
    expect(resolveIn({ anchorPath: "div:nth-of-type(1)>p:nth-of-type(7)" }, asDoc)).toBeNull();
    expect(resolveIn({ anchorPath: "not a path" }, asDoc)).toBeNull();
    expect(resolveIn({ anchorPath: "" }, asDoc)).toBeNull();
  });

  test("body itself is not an answer — a note about the whole page is about nothing", () => {
    expect(pathOf(body as unknown as Element, asDoc)).toBe("");
    expect(resolveIn({ anchorPath: "" }, asDoc)).toBeNull();
  });

  test("no document, no answer", () => {
    expect(resolveIn({ anchorId: "hit" }, null)).toBeNull();
  });
});
