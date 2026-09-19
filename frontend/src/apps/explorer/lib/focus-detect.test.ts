// The pure decision behind the home-page focus-change-detection trigger.
import { describe, expect, it } from "bun:test";
import { MIN_HIDDEN_MS, hiddenSeconds, isAway, shouldNoteFocus } from "./focus-detect";

describe("shouldNoteFocus", () => {
  it("is false with no recorded hidden-since (never observed going hidden)", () => {
    expect(shouldNoteFocus(null)).toBe(false);
  });

  it("is false when hidden for less than the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS - 1)).toBe(false);
  });

  it("is true exactly at the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS)).toBe(true);
  });

  it("is true well past the floor", () => {
    expect(shouldNoteFocus(MIN_HIDDEN_MS * 100)).toBe(true);
  });

  it("is false for a brief alt-tab between this app's own windows", () => {
    expect(shouldNoteFocus(500)).toBe(false);
  });
});

describe("hiddenSeconds", () => {
  it("rounds milliseconds to the nearest whole second", () => {
    expect(hiddenSeconds(45_000)).toBe(45);
    expect(hiddenSeconds(45_400)).toBe(45);
    expect(hiddenSeconds(45_600)).toBe(46);
  });
});

// Minimal stand-ins for Document/Window — only the members isAway() reads.
function fakeDoc(opts: { hidden?: boolean; hasFocus: boolean }): Document {
  return {
    hidden: opts.hidden ?? false,
    hasFocus: () => opts.hasFocus,
  } as unknown as Document;
}

function notFramed(win: unknown): Window {
  const w = win as { top?: unknown };
  w.top = win;
  return win as Window;
}

function framedWith(topDoc: Document | null, ownWin: unknown): Window {
  const w = ownWin as { top?: unknown };
  if (topDoc === null) {
    // Simulates a cross-origin ancestor: reading `.document` off it throws.
    w.top = {
      get document(): Document {
        throw new DOMException("Blocked a frame with origin", "SecurityError");
      },
    };
  } else {
    w.top = { document: topDoc };
  }
  return ownWin as Window;
}

describe("isAway", () => {
  it("is away when the document itself is hidden, regardless of focus", () => {
    const doc = fakeDoc({ hidden: true, hasFocus: false });
    const win = notFramed({});
    expect(isAway(doc, win)).toBe(true);
  });

  it("is not away, unframed, when this frame has focus", () => {
    const doc = fakeDoc({ hasFocus: true });
    const win = notFramed({});
    expect(isAway(doc, win)).toBe(false);
  });

  it("is away, unframed, when this frame has lost focus (plain browser tab / dev build)", () => {
    const doc = fakeDoc({ hasFocus: false });
    const win = notFramed({});
    expect(isAway(doc, win)).toBe(true);
  });

  it("is NOT away when this frame lost focus to a sibling pane in the same app shell " +
      "(finding 8: the whole point of the fix)", () => {
    // This frame's own hasFocus() is false (focus moved to the sidebar), but
    // the outer shell document still has focus somewhere in its subtree.
    const ownDoc = fakeDoc({ hasFocus: false });
    const topDoc = fakeDoc({ hasFocus: true });
    const win = framedWith(topDoc, {});
    expect(isAway(ownDoc, win)).toBe(false);
  });

  it("IS away when the whole app window loses OS focus to a different application", () => {
    const ownDoc = fakeDoc({ hasFocus: false });
    const topDoc = fakeDoc({ hasFocus: false });
    const win = framedWith(topDoc, {});
    expect(isAway(ownDoc, win)).toBe(true);
  });

  it("falls back to this frame's own hasFocus() when the ancestor is cross-origin " +
      "(should not happen for this app's own shell, but must not throw)", () => {
    const ownDoc = fakeDoc({ hasFocus: true });
    const win = framedWith(null, {});
    expect(isAway(ownDoc, win)).toBe(false);
  });
});
