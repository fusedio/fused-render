// Quick look's Shift (owner, 2026-11-10; design.md "Quick look v2"): the hold,
// its releases — a lost keyup included — and Escape sending every window back
// to its pinned chat with the hold going on.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { installQuickLookKeys, shiftHolds, type HoldEvent } from "./quick-look-keys";
import { quickLookHeld, registerPreview, releaseQuickLook, resetQuickLookForTests, revertPreviews, holdQuickLook } from "./quick-look-store";

const key = (o: Partial<HoldEvent & { defaultPrevented: boolean }> = {}) => {
  const e = {
    key: "Shift",
    repeat: false,
    shiftKey: true,
    isComposing: false,
    defaultPrevented: false,
    stopped: false,
    ...o,
    preventDefault() {
      e.defaultPrevented = true;
    },
    stopPropagation() {
      e.stopped = true;
    },
  };
  return e;
};

// A window and a document as the layer listens to them.
class Host {
  handlers = new Map<string, Set<(e: Event) => void>>();
  addEventListener(type: string, fn: (e: Event) => void) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set());
    this.handlers.get(type)!.add(fn);
  }
  removeEventListener(type: string, fn: (e: Event) => void) {
    this.handlers.get(type)?.delete(fn);
  }
  fire(type: string, e: object = {}) {
    for (const fn of [...(this.handlers.get(type) ?? [])]) fn(e as Event);
  }
}

class FakeDoc extends Host {
  visibilityState: "visible" | "hidden" = "visible";
}

/** A window's preview, as quick-look-store sees one. */
const fakePreview = (at: string | null = null) => {
  const p = {
    current: at,
    clears: 0,
    clear() {
      p.current = null;
      p.clears += 1;
    },
  };
  return p;
};

describe("shiftHolds", () => {
  it("Shift, left or right, holds; a repeat or an IME composition does not", () => {
    expect(shiftHolds(key())).toBe(true);
    expect(shiftHolds(key({ repeat: true }))).toBe(false);
    expect(shiftHolds(key({ isComposing: true }))).toBe(false);
    expect(shiftHolds(key({ key: "a" }))).toBe(false);
  });
});

describe("installQuickLookKeys", () => {
  let win: Host;
  let doc: FakeDoc;
  let off: () => void;
  beforeEach(() => {
    resetQuickLookForTests();
    win = new Host();
    doc = new FakeDoc();
    off = installQuickLookKeys(win, doc as unknown as Document);
  });
  afterEach(() => off());

  it("Shift down holds — taking nothing from the page; Shift up releases", () => {
    const down = key();
    win.fire("keydown", down);
    expect(quickLookHeld()).toBe(true);
    expect(down.defaultPrevented).toBe(false);
    win.fire("keyup", key({ shiftKey: false }));
    expect(quickLookHeld()).toBe(false);
  });

  it("the auto-repeat changes nothing", () => {
    win.fire("keydown", key({ repeat: true }));
    expect(quickLookHeld()).toBe(false);
    win.fire("keydown", key());
    win.fire("keydown", key({ repeat: true }));
    expect(quickLookHeld()).toBe(true);
  });

  it("typing with Shift down keeps the hold (a capital letter is Shift held)", () => {
    win.fire("keydown", key());
    win.fire("keydown", key({ key: "A" }));
    win.fire("keyup", key({ key: "A" }));
    expect(quickLookHeld()).toBe(true);
  });

  it("a LOST keyup: any key or pointer event without Shift releases", () => {
    win.fire("keydown", key());
    win.fire("keydown", key({ key: "a", shiftKey: false }));
    expect(quickLookHeld()).toBe(false);
    win.fire("keydown", key());
    win.fire("keyup", key({ key: "a", shiftKey: false }));
    expect(quickLookHeld()).toBe(false);
    win.fire("keydown", key());
    win.fire("pointermove", { shiftKey: true });
    expect(quickLookHeld()).toBe(true);
    win.fire("pointermove", { shiftKey: false });
    expect(quickLookHeld()).toBe(false);
  });

  it("a window blur or the page going hidden releases", () => {
    win.fire("keydown", key());
    win.fire("blur");
    expect(quickLookHeld()).toBe(false);
    win.fire("keydown", key());
    doc.visibilityState = "hidden";
    doc.fire("visibilitychange");
    expect(quickLookHeld()).toBe(false);
  });

  it("the release sends every window back to pinned", () => {
    const a = fakePreview("Y");
    const b = fakePreview("G");
    registerPreview(a);
    registerPreview(b);
    win.fire("keydown", key());
    win.fire("keyup", key({ shiftKey: false }));
    expect([a.current, b.current]).toEqual([null, null]);
  });

  it("Escape while previewing: back to pinned, the hold goes on, the key is nobody else's", () => {
    const a = fakePreview("Y");
    registerPreview(a);
    win.fire("keydown", key());
    const esc = key({ key: "Escape" });
    win.fire("keydown", esc);
    expect(a.current).toBeNull();
    expect(esc.defaultPrevented).toBe(true);
    expect(esc.stopped).toBe(true);
    expect(quickLookHeld()).toBe(true);
    // With nothing previewing, Escape is the page's.
    const again = key({ key: "Escape" });
    win.fire("keydown", again);
    expect(again.defaultPrevented).toBe(false);
  });

  it("uninstalling lets a hold go", () => {
    holdQuickLook();
    off();
    expect(quickLookHeld()).toBe(false);
    off = () => {};
  });
});

describe("quick-look-store", () => {
  beforeEach(() => resetQuickLookForTests());

  it("revertPreviews says whether any window was previewing", () => {
    const a = fakePreview();
    const off = registerPreview(a);
    expect(revertPreviews()).toBe(false);
    a.current = "Y";
    expect(revertPreviews()).toBe(true);
    off();
    a.current = "Y";
    releaseQuickLook();
    // Unregistered: no longer reached.
    expect(a.current).toBe("Y");
  });
});
