// Left sidebar preview's store (left-peek-store): one card per collapsed-sidebar
// icon, the rules between them, and none of it touching the Claude rail.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import type { PointerEvent as ReactPointerEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import { aPinned, aPins, setAPinned } from "@platform/lib/last-chat";
import { PEEK_OPEN_MS, type Timers } from "@shell/claude-rail/rail-peek-lib";
import { resetRailExtrasFlagForTests, setRailExtrasForTests } from "@shell/claude-rail/rail-extras-flag";
import {
  closeLeftCards,
  leftCardStore,
  leftDoorFor,
  leftMove,
  leftPeekSnapshot,
  publishLeftItems,
  resetLeftPeekForTests,
  setLeftPeekBlocked,
  setLeftRail,
  leftSidebarFolded,
  leftKeyOpens,
  type LeftPeekItem,
} from "./left-peek-store";

class Clock implements Timers {
  now = 0;
  private id = 0;
  private timers = new Map<number, { at: number; fn: () => void }>();
  set(fn: () => void, ms: number): number {
    const id = ++this.id;
    this.timers.set(id, { at: this.now + ms, fn });
    return id;
  }
  clear(id: number): void {
    this.timers.delete(id);
  }
  tick(ms: number): void {
    const end = this.now + ms;
    for (;;) {
      const next = [...this.timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!next) break;
      this.timers.delete(next[0]);
      this.now = next[1].at;
      next[1].fn();
    }
    this.now = end;
  }
}

const item = (key: string): LeftPeekItem => ({ key, title: key, icon: null, show: { kind: "frame", src: "/x/" + key }, onOpen: () => {} });
const enter = (key: string, e: Partial<PointerEvent> = {}) =>
  leftDoorFor(key)!.onPointerEnter!({ pointerType: "mouse", buttons: 0, ...e } as unknown as ReactPointerEvent<HTMLElement>);
/** A card ready to stand (DoorCard registered, room beside the rail). */
const ready = (key: string) => leftCardStore(key).registerCard();
const state = (key: string) => leftCardStore(key).state();
/** A press inside the card (DoorCard's capture handler). */
const pressIn = (key: string) => {
  const i = leftCardStore(key).intent();
  i.pressStart();
  i.pressEnd();
};

let clock: Clock;
const offs: (() => void)[] = [];
beforeEach(() => {
  clock = new Clock();
  resetLeftPeekForTests(clock);
  resetRailExtrasFlagForTests();
  setAPinned(null);
});
afterEach(() => {
  for (const off of offs.splice(0)) off();
  resetRailExtrasFlagForTests();
});

describe("the icons' facts", () => {
  it("no hooks on an icon that is not published", () => {
    expect(leftDoorFor("home")).toBeUndefined();
  });
  it("a published icon has its card and wears its door; the tooltip is quiet once the card can stand", () => {
    publishLeftItems([item("home"), item("app:/w/a")]);
    expect(leftPeekSnapshot().keys).toEqual(["home", "app:/w/a"]);
    const before = leftDoorFor("home")!;
    expect(before.attrs).toEqual({ "data-left-door": "home" });
    expect(before.quietTip).toBe(false);
    offs.push(ready("home"));
    const after = leftDoorFor("home")!;
    expect(after.attrs?.["aria-controls"]).toBe(leftCardStore("home").id);
    expect(after.attrs?.["aria-expanded"]).toBe("false");
    expect(after.quietTip).toBe(true);
  });
  it("an icon that goes takes its card with it — held too", () => {
    publishLeftItems([item("home"), item("pin:1")]);
    offs.push(ready("pin:1"));
    enter("pin:1");
    clock.tick(PEEK_OPEN_MS);
    pressIn("pin:1");
    leftCardStore("pin:1").setHeld(true);
    expect(state("pin:1").held).toBe(true);
    publishLeftItems([item("home")]);
    expect(state("pin:1").up).toBe(false);
    expect(leftDoorFor("pin:1")).toBeUndefined();
  });
});

describe("the hover", () => {
  it("opens after the usual wait (the left zone unwatched), never with no card to stand", () => {
    publishLeftItems([item("home")]);
    enter("home");
    clock.tick(PEEK_OPEN_MS);
    expect(state("home").up).toBe(false);
    offs.push(ready("home"));
    enter("home");
    clock.tick(PEEK_OPEN_MS - 1);
    expect(state("home").up).toBe(false);
    clock.tick(1);
    expect(state("home").up).toBe(true);
  });
  it("an entry that becomes the active tab's before its hover's delay is up opens nothing", () => {
    publishLeftItems([item("home"), item("app:/w/a")]);
    offs.push(ready("app:/w/a"));
    enter("app:/w/a");
    clock.tick(PEEK_OPEN_MS - 1);
    publishLeftItems([item("home"), { ...item("app:/w/a"), active: true }]);
    clock.tick(PEEK_OPEN_MS);
    expect(state("app:/w/a").up).toBe(false);
  });
  it("a touch, a drag (buttons down) and the Settings menu open nothing", () => {
    publishLeftItems([item("home")]);
    offs.push(ready("home"));
    enter("home", { pointerType: "touch" });
    enter("home", { buttons: 1 });
    clock.tick(PEEK_OPEN_MS * 4);
    expect(state("home").up).toBe(false);
    setLeftPeekBlocked(true);
    enter("home");
    clock.tick(PEEK_OPEN_MS * 4);
    expect(state("home").up).toBe(false);
  });
  it("Escape on the icon lets a preview go, never a held card", () => {
    publishLeftItems([item("home")]);
    offs.push(ready("home"));
    enter("home");
    clock.tick(PEEK_OPEN_MS);
    let prevented = false;
    const key = { key: "Escape", preventDefault: () => (prevented = true) } as unknown as ReactKeyboardEvent<HTMLElement>;
    leftDoorFor("home")!.onKeyDown!(key);
    expect(prevented).toBe(true);
    expect(state("home").up).toBe(false);
    enter("home");
    clock.tick(PEEK_OPEN_MS);
    pressIn("home");
    leftCardStore("home").setHeld(true);
    leftDoorFor("home")!.onKeyDown!(key);
    expect(state("home").up).toBe(true);
  });
});

describe("the keyboard's way in (ArrowRight)", () => {
  it("a bare ArrowRight, on an entry whose card can stand, not from a field, nothing else holding the sidebar", () => {
    const base = { key: "ArrowRight", modified: false, ready: true, editable: false, blocked: false };
    expect(leftKeyOpens(base)).toBe(true);
    expect(leftKeyOpens({ ...base, key: "Enter" })).toBe(false);
    expect(leftKeyOpens({ ...base, key: "ArrowDown" })).toBe(false);
    expect(leftKeyOpens({ ...base, modified: true })).toBe(false);
    expect(leftKeyOpens({ ...base, ready: false })).toBe(false);
    expect(leftKeyOpens({ ...base, editable: true })).toBe(false);
    expect(leftKeyOpens({ ...base, blocked: true })).toBe(false);
  });
  it("opens the card temp at once; Enter does nothing here (the link navigates); the door says its shortcut", () => {
    publishLeftItems([item("home")]);
    offs.push(ready("home"));
    expect(leftDoorFor("home")!.attrs?.["aria-keyshortcuts"]).toBe("ArrowRight");
    let prevented = 0;
    const key = (k: string) =>
      ({ key: k, target: null, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, preventDefault: () => prevented++ }) as unknown as ReactKeyboardEvent<HTMLElement>;
    leftDoorFor("home", "row")!.onKeyDown!(key("Enter"));
    expect(state("home").up).toBe(false);
    expect(prevented).toBe(0);
    leftDoorFor("home", "row")!.onKeyDown!(key("ArrowRight"));
    expect(prevented).toBe(1);
    expect(state("home")).toMatchObject({ up: true, pinned: true, held: false });
  });
  it("no shortcut on an entry with no card to stand", () => {
    publishLeftItems([item("home")]);
    expect(leftDoorFor("home")!.attrs?.["aria-keyshortcuts"]).toBeUndefined();
  });
});

describe("the rules between the cards", () => {
  beforeEach(() => {
    publishLeftItems([item("home"), item("ai-models"), item("marketplace")]);
    for (const k of ["home", "ai-models", "marketplace"]) offs.push(ready(k));
  });
  const open = (key: string) => {
    leftDoorFor(key)!.onPointerLeave!({} as ReactPointerEvent<HTMLElement>);
    enter(key);
    clock.tick(PEEK_OPEN_MS);
  };
  it("one non-held card at a time: a card coming up puts a temp one down, never a held one", () => {
    open("home");
    pressIn("home");
    expect(state("home").pinned).toBe(true);
    open("ai-models");
    expect(state("ai-models").up).toBe(true);
    expect(state("home").up).toBe(false);
    pressIn("ai-models");
    leftCardStore("ai-models").setHeld(true);
    open("marketplace");
    expect(state("marketplace").up).toBe(true);
    expect(state("ai-models").up).toBe(true);
  });
  it("the icon's click puts its own card down unless held", () => {
    open("home");
    leftDoorFor("home")!.onActivate!();
    expect(state("home").up).toBe(false);
    open("home");
    pressIn("home");
    leftCardStore("home").setHeld(true);
    leftDoorFor("home")!.onActivate!();
    expect(state("home").up).toBe(true);
  });
  it("the Settings menu opening puts down every card that is not held", () => {
    open("home");
    setLeftPeekBlocked(true);
    expect(state("home").up).toBe(false);
  });
  it("the sidebar going (the switch off, an embed) puts down the non-held cards and a held one with nowhere to float", () => {
    const nav = {} as HTMLElement;
    setLeftRail(nav);
    open("home");
    pressIn("home");
    leftCardStore("home").setHeld(true);
    open("ai-models");
    pressIn("ai-models");
    leftCardStore("ai-models").setHeld(true);
    leftMove.setPos(leftCardStore("ai-models").key, { left: 300, top: 200 });
    open("marketplace");
    setLeftRail(null);
    expect(state("marketplace").up).toBe(false);
    expect(state("home").up).toBe(false);
    expect(state("ai-models").up).toBe(true);
  });
  it("the sidebar collapsing or expanding puts down every card that is not held; a held one stays", () => {
    open("home");
    pressIn("home");
    leftCardStore("home").setHeld(true);
    open("ai-models");
    leftSidebarFolded();
    expect(state("ai-models").up).toBe(false);
    expect(state("home").up).toBe(true);
  });
  it("a press on the entry puts its own card down unless held; its context menu puts down every card that is not held", () => {
    open("home");
    leftDoorFor("home", "row")!.onPointerDown!({} as ReactPointerEvent<HTMLElement>);
    expect(state("home").up).toBe(false);
    open("ai-models");
    leftDoorFor("home", "row")!.onContextMenu!({} as never);
    expect(state("ai-models").up).toBe(false);
    open("marketplace");
    pressIn("marketplace");
    leftCardStore("marketplace").setHeld(true);
    leftDoorFor("marketplace", "row")!.onPointerDown!({} as ReactPointerEvent<HTMLElement>);
    expect(state("marketplace").up).toBe(true);
  });
  it("the entry whose card is up wears it: open, then pinned once held", () => {
    expect(leftDoorFor("home")!.attrs?.["data-left-open"]).toBeUndefined();
    open("home");
    expect(leftDoorFor("home")!.attrs?.["data-left-open"]).toBe("open");
    expect(leftDoorFor("home", "row")!.attrs?.["data-left-open"]).toBe("open");
    pressIn("home");
    leftCardStore("home").setHeld(true);
    expect(leftDoorFor("home")!.attrs?.["data-left-open"]).toBe("pinned");
  });
  it("one key, two slots: the rail icon and the expanded row are two refs of one card", () => {
    expect(leftDoorFor("home", "rail")!.ref).not.toBe(leftDoorFor("home", "row")!.ref);
    expect(leftDoorFor("home", "row")!.ref).toBe(leftDoorFor("home", "row")!.ref);
  });
  it("a card losing its hold while up is non-held again: the other non-held card goes (free pins off)", () => {
    setRailExtrasForTests(false);
    open("home");
    pressIn("home");
    leftCardStore("home").setHeld(true);
    open("ai-models");
    pressIn("ai-models");
    expect(state("home").up && state("ai-models").up).toBe(true);
    leftCardStore("home").setHeld(false);
    expect(state("home").up).toBe(true);
    expect(state("ai-models").up).toBe(false);
  });
  it("a hover still waiting on its delay opens nothing once the Settings menu, a context menu or a fold took the sidebar", () => {
    for (const busy of [
      () => setLeftPeekBlocked(true),
      () => leftDoorFor("marketplace")!.onContextMenu!({} as never),
      () => leftSidebarFolded(),
    ]) {
      setLeftPeekBlocked(false);
      leftDoorFor("home")!.onPointerLeave!({} as ReactPointerEvent<HTMLElement>);
      enter("home");
      clock.tick(PEEK_OPEN_MS - 10);
      busy();
      clock.tick(PEEK_OPEN_MS);
      expect(state("home").up).toBe(false);
    }
  });
  it("closeLeftCards keeps what it is asked to keep", () => {
    open("home");
    closeLeftCards((k) => k === "home");
    expect(state("home").up).toBe(true);
    closeLeftCards();
    expect(state("home").up).toBe(false);
  });
});

describe("never the Claude rail's", () => {
  it("a held left card writes no window A mark, either pin model", () => {
    publishLeftItems([item("home")]);
    offs.push(ready("home"));
    for (const free of [false, true]) {
      setRailExtrasForTests(free);
      enter("home");
      clock.tick(PEEK_OPEN_MS);
      pressIn("home");
      leftCardStore("home").setHeld(true);
      expect(aPinned()).toBeNull();
      expect(aPins()).toEqual([]);
      leftCardStore("home").close();
      expect(aPinned()).toBeNull();
    }
  });
  it("with free pins a closing card forgets where it floated; without, the spot is kept for the next pin", () => {
    publishLeftItems([item("home")]);
    offs.push(ready("home"));
    const k = leftCardStore("home").key;
    setRailExtrasForTests(false);
    enter("home");
    clock.tick(PEEK_OPEN_MS);
    leftMove.setPos(k, { left: 10, top: 10 });
    leftCardStore("home").close();
    expect(leftMove.pos(k)).toEqual({ left: 10, top: 10 });
    setRailExtrasForTests(true);
    enter("home");
    clock.tick(PEEK_OPEN_MS);
    leftCardStore("home").close();
    expect(leftMove.pos(k)).toBeNull();
  });
});
