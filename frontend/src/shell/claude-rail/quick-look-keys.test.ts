// Quick look's Space key (owner, 2026-11-10; design.md "Space quick look"):
// when Space is a hold and when it is still Space, and the releases.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { acquireOverlay, isOverlayOpen, releaseOverlay } from "@platform/lib/ui-overlay";
import { BLOCKING_DIALOGS, installQuickLookKeys, isSpaceTarget, spaceHolds, type KeyTarget, type SpaceKey } from "./quick-look-keys";
import { aimQuickLook, enterQuickLookCard, holdQuickLook, quickLookState, resetQuickLookForTests } from "./quick-look-store";
import type { Timers } from "./rail-peek-lib";

const space = (o: Partial<SpaceKey> = {}): SpaceKey => ({
  key: " ",
  repeat: false,
  ctrlKey: false,
  metaKey: false,
  altKey: false,
  shiftKey: false,
  isComposing: false,
  defaultPrevented: false,
  ...o,
});

const el = (tagName: string, attrs: Record<string, string> = {}, parentElement: KeyTarget | null = null): KeyTarget => ({
  tagName,
  isContentEditable: false,
  getAttribute: (n) => attrs[n] ?? null,
  classList: { contains: (c) => (attrs.class ?? "").split(" ").includes(c) },
  parentElement,
});

const body = el("BODY");
const ok = { active: body, blocked: false, railUp: true };

describe("spaceHolds", () => {
  it("a bare Space with the focus on the page holds", () => {
    expect(spaceHolds(space(), ok)).toBe(true);
    expect(spaceHolds(space(), { ...ok, active: null })).toBe(true);
  });

  it("not another key, not a repeat, not with a modifier, not mid-composition or taken", () => {
    expect(spaceHolds(space({ key: "a" }), ok)).toBe(false);
    expect(spaceHolds(space({ repeat: true }), ok)).toBe(false);
    for (const m of ["ctrlKey", "metaKey", "altKey", "shiftKey"] as const) {
      expect(spaceHolds(space({ [m]: true }), ok)).toBe(false);
    }
    expect(spaceHolds(space({ isComposing: true }), ok)).toBe(false);
    expect(spaceHolds(space({ defaultPrevented: true }), ok)).toBe(false);
  });

  it("not with the caret in a field", () => {
    for (const tag of ["INPUT", "TEXTAREA", "SELECT"]) {
      expect(spaceHolds(space(), { ...ok, active: el(tag) })).toBe(false);
    }
    const editable: KeyTarget = { ...el("DIV"), isContentEditable: true };
    expect(spaceHolds(space(), { ...ok, active: editable })).toBe(false);
  });

  it("not on something Space presses", () => {
    expect(spaceHolds(space(), { ...ok, active: el("BUTTON") })).toBe(false);
    expect(spaceHolds(space(), { ...ok, active: el("A", { href: "/x" }) })).toBe(false);
    expect(spaceHolds(space(), { ...ok, active: el("DIV", { role: "button" }) })).toBe(false);
    expect(spaceHolds(space(), { ...ok, active: el("DIV", { contenteditable: "true" }) })).toBe(false);
    // …or inside one.
    expect(spaceHolds(space(), { ...ok, active: el("SPAN", {}, el("DIV", { role: "button" })) })).toBe(false);
    // A plain focusable box is not a control.
    expect(spaceHolds(space(), { ...ok, active: el("DIV", { tabindex: "-1" }) })).toBe(true);
    expect(isSpaceTarget(el("DIV", { contenteditable: "false" }))).toBe(false);
  });

  it("inside the rail, a focused ring (an <a>) still holds; a field there does not", () => {
    const rail = el("ASIDE", { class: "claude-rail has-favs" });
    const ring = el("A", { href: "/chat", class: "claude-rail-chat" }, el("DIV", { role: "listitem" }, rail));
    expect(spaceHolds(space(), { ...ok, active: ring })).toBe(true);
    expect(spaceHolds(space(), { ...ok, active: el("TEXTAREA", {}, rail) })).toBe(false);
    // A link elsewhere keeps its Space.
    expect(spaceHolds(space(), { ...ok, active: el("A", { href: "/x" }, el("NAV")) })).toBe(false);
  });

  it("not with a dialog or overlay open, nor with no rail to aim at", () => {
    expect(spaceHolds(space(), { ...ok, blocked: true })).toBe(false);
    expect(spaceHolds(space(), { ...ok, railUp: false })).toBe(false);
  });
});

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
  activeElement: KeyTarget | null = body;
  visibilityState: "visible" | "hidden" = "visible";
  rail = true;
  /** A blocking dialog in the DOM: none, drawn, or kept hidden. */
  modal: "none" | "shown" | "hidden" = "none";
  querySelector(sel: string) {
    if (sel === ".claude-rail") return this.rail ? {} : null;
    throw new Error(`unexpected selector ${sel}`);
  }
  querySelectorAll(sel: string) {
    if (sel !== BLOCKING_DIALOGS) throw new Error(`unexpected selector ${sel}`);
    if (this.modal === "none") return [];
    const rects = this.modal === "shown" ? [{}] : [];
    return [{ getClientRects: () => rects }];
  }
}

const press = (o: Partial<SpaceKey> = {}) => {
  const e = {
    ...space(o),
    stopped: false,
    preventDefault() {
      e.defaultPrevented = true;
    },
    stopPropagation() {
      e.stopped = true;
    },
  };
  return e;
};

class Clock implements Timers {
  fns: (() => void)[] = [];
  set(fn: () => void) {
    this.fns.push(fn);
    return this.fns.length;
  }
  clear() {}
  flush() {
    for (const fn of this.fns.splice(0)) fn();
  }
}

describe("installQuickLookKeys", () => {
  let win: Host;
  let doc: FakeDoc;
  let off: () => void;
  let clock: Clock;
  beforeEach(() => {
    clock = new Clock();
    resetQuickLookForTests(clock);
    win = new Host();
    doc = new FakeDoc();
    off = installQuickLookKeys(win, doc as unknown as Document);
  });
  afterEach(() => {
    off();
    while (isOverlayOpen()) releaseOverlay();
  });

  const open = () => {
    aimQuickLook("a", {
      anchor: { left: 0, top: 0, width: 1, height: 1 },
      chat: { file: "/p", sessionId: "s", title: "a", status: "done" },
    });
    clock.flush();
  };

  it("Space down holds and takes the key; Space up releases", () => {
    const down = press();
    win.fire("keydown", down);
    expect(down.defaultPrevented).toBe(true);
    expect(quickLookState().held).toBe(true);
    // The auto-repeat is taken too (no page scroll under the card).
    const again = press({ repeat: true });
    win.fire("keydown", again);
    expect(again.defaultPrevented).toBe(true);
    win.fire("keyup", press());
    expect(quickLookState().held).toBe(false);
  });

  it("leaves Space alone in a field, on a control, under a dialog or overlay, or with no rail", () => {
    doc.activeElement = el("TEXTAREA");
    let e = press();
    win.fire("keydown", e);
    expect(e.defaultPrevented).toBe(false);
    doc.activeElement = el("BUTTON");
    win.fire("keydown", (e = press()));
    expect(e.defaultPrevented).toBe(false);
    doc.activeElement = body;
    doc.modal = "shown";
    win.fire("keydown", (e = press()));
    expect(e.defaultPrevented).toBe(false);
    doc.modal = "none";
    acquireOverlay();
    win.fire("keydown", (e = press()));
    expect(e.defaultPrevented).toBe(false);
    releaseOverlay();
    doc.rail = false;
    win.fire("keydown", (e = press()));
    expect(e.defaultPrevented).toBe(false);
    expect(quickLookState().held).toBe(false);
  });

  it("a window blur or the page going hidden releases", () => {
    win.fire("keydown", press());
    open();
    expect(quickLookState().key).toBe("a");
    win.fire("blur");
    expect(quickLookState()).toMatchObject({ held: false, key: null });
    win.fire("keydown", press());
    doc.visibilityState = "hidden";
    doc.fire("visibilitychange");
    expect(quickLookState().held).toBe(false);
  });

  it("Escape closes the card, keeps the hold, and is nobody else's", () => {
    win.fire("keydown", press());
    open();
    const esc = press({ key: "Escape" });
    win.fire("keydown", esc);
    expect(esc.defaultPrevented).toBe(true);
    expect(esc.stopped).toBe(true);
    expect(quickLookState()).toMatchObject({ held: true, key: null });
    // With nothing shown, Escape is the page's again.
    const esc2 = press({ key: "Escape" });
    win.fire("keydown", esc2);
    expect(esc2.defaultPrevented).toBe(false);
  });

  it("a dialog kept in the DOM but hidden does not block", () => {
    doc.modal = "hidden";
    const e = press();
    win.fire("keydown", e);
    expect(e.defaultPrevented).toBe(true);
    expect(quickLookState().held).toBe(true);
  });

  it("a fresh Space down while held is a stale hold: released, then guarded as a first press", () => {
    win.fire("keydown", press());
    open();
    doc.activeElement = el("TEXTAREA");
    const e = press();
    win.fire("keydown", e);
    expect(e.defaultPrevented).toBe(false);
    expect(quickLookState()).toMatchObject({ held: false, key: null });
    // …and on the page, it holds again.
    doc.activeElement = body;
    const again = press();
    win.fire("keydown", again);
    expect(again.defaultPrevented).toBe(true);
    expect(quickLookState().held).toBe(true);
  });

  it("⌘ after Space (macOS loses the Space up): the ⌘ keydown releases", () => {
    win.fire("keydown", press());
    open();
    win.fire("keydown", press({ key: "Meta", metaKey: true }));
    expect(quickLookState()).toMatchObject({ held: false, key: null });
  });

  it("…and so does a keyup carrying ⌘", () => {
    win.fire("keydown", press());
    win.fire("keyup", press({ key: "c", metaKey: true }));
    expect(quickLookState().held).toBe(false);
  });

  it("a Space up with no hold leaves the default alone", () => {
    const up = press();
    win.fire("keyup", up);
    expect(up.defaultPrevented).toBe(false);
  });

  it("Escape calls off an aim still waiting, and leaves the key to the page", () => {
    win.fire("keydown", press());
    aimQuickLook("a", {
      anchor: { left: 0, top: 0, width: 1, height: 1 },
      chat: { file: "/p", sessionId: "s", title: "a", status: "done" },
    });
    const esc = press({ key: "Escape" });
    win.fire("keydown", esc);
    expect(esc.defaultPrevented).toBe(false);
    clock.flush();
    expect(quickLookState()).toMatchObject({ held: true, key: null });
  });

  it("Space up or a blur closes it with the pointer on the card", () => {
    win.fire("keydown", press());
    open();
    enterQuickLookCard();
    win.fire("keyup", press());
    expect(quickLookState()).toMatchObject({ held: false, key: null });
    win.fire("keydown", press());
    open();
    enterQuickLookCard();
    win.fire("blur");
    expect(quickLookState()).toMatchObject({ held: false, key: null });
  });

  it("uninstalling lets a hold go", () => {
    holdQuickLook();
    off();
    expect(quickLookState().held).toBe(false);
    off = () => {};
  });
});
