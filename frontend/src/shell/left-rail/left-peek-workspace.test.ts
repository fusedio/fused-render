// Left sidebar preview in a workspace host (SPEC §56 WS-28…WS-31): only a
// favourite's card pins and opens on a click; the entry the active tab shows
// opens no card; the Search row's click is the launcher, else its card.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import type { PointerEvent as ReactPointerEvent, KeyboardEvent as ReactKeyboardEvent } from "react";
import { PEEK_OPEN_MS, type Timers } from "@shell/claude-rail/rail-peek-lib";
import {
  leftCardOnHover,
  leftCardPinnable,
  leftEntryPress,
  leftSearchRowShown,
  pressOnLink,
  shiftClickBookmark,
} from "./left-peek-lib";
import { leftCardStore, leftDoorFor, publishLeftItems, resetLeftPeekForTests, type LeftPeekItem } from "./left-peek-store";

describe("what a click on a left entry does", () => {
  const base = { workspace: true, peekOn: true, kind: "bookmark" as const, hasCard: true, active: false, launcher: false };
  it("a favourite in a host with Left sidebar preview on: its card", () => {
    expect(leftEntryPress(base)).toBe("card");
  });
  it("a favourite opens its page outside a host, with the preview off, with no card, or when the active tab shows it", () => {
    expect(leftEntryPress({ ...base, workspace: false })).toBe("page");
    expect(leftEntryPress({ ...base, peekOn: false })).toBe("page");
    expect(leftEntryPress({ ...base, hasCard: false })).toBe("page");
    expect(leftEntryPress({ ...base, active: true })).toBe("page");
  });
  it("every other entry: its page, host or not", () => {
    expect(leftEntryPress({ ...base, kind: "other" })).toBe("page");
    expect(leftEntryPress({ ...base, kind: "other", workspace: false })).toBe("page");
  });
  it("Search: the launcher where there is one; in a host without one, its card", () => {
    expect(leftEntryPress({ ...base, kind: "search", launcher: true })).toBe("launcher");
    expect(leftEntryPress({ ...base, kind: "search" })).toBe("card");
    expect(leftEntryPress({ ...base, kind: "search", workspace: false })).toBe("launcher");
  });
});

describe("the other rules", () => {
  it("the Search row stands with the launcher, or in a host with the preview on", () => {
    expect(leftSearchRowShown({ workspace: false, launcher: true, peekOn: false })).toBe(true);
    expect(leftSearchRowShown({ workspace: false, launcher: false, peekOn: true })).toBe(false);
    expect(leftSearchRowShown({ workspace: true, launcher: false, peekOn: true })).toBe(true);
    expect(leftSearchRowShown({ workspace: true, launcher: false, peekOn: false })).toBe(false);
  });
  it("a pin on every card outside a host; in one, only a favourite's", () => {
    expect(leftCardPinnable({ workspace: false, kind: "other" })).toBe(true);
    expect(leftCardPinnable({ workspace: true, kind: "bookmark" })).toBe(true);
    expect(leftCardPinnable({ workspace: true, kind: "other" })).toBe(false);
    expect(leftCardPinnable({ workspace: true, kind: "search" })).toBe(false);
  });
  it("the entry the active tab shows opens no card, in a host only", () => {
    expect(leftCardOnHover({ workspace: true, active: true })).toBe(false);
    expect(leftCardOnHover({ workspace: true, active: false })).toBe(true);
    expect(leftCardOnHover({ workspace: false, active: true })).toBe(true);
  });
});

type El = { sel: string[]; parent: El | null; attrs?: Record<string, string> };
const el = (sel: string[], parent: El | null = null, attrs: Record<string, string> = {}): El => ({ sel, parent, attrs });
const dom = (e: El) => ({
  closest(s: string): unknown {
    for (let at: El | null = e; at; at = at.parent) if (s.split(",").some((one) => at!.sel.includes(one.trim()))) return dom(at);
    return null;
  },
  matches: (s: string) => e.sel.includes(s),
  getAttribute: (n: string) => e.attrs?.[n] ?? null,
  el: e,
});

describe("where a press landed", () => {
  it("on the entry's own link or icon: a click's press; on a row's other buttons: not", () => {
    const row = el(["row"]);
    const link = el(["a[href]"], row);
    const kebab = el(["button"], row);
    const glyph = el(["span"], row);
    const rowNode = dom(row);
    expect(pressOnLink(dom(link), rowNode)).toBe(true);
    expect(pressOnLink(dom(kebab), rowNode)).toBe(false);
    expect(pressOnLink(dom(glyph), rowNode)).toBe(false);
    expect(pressOnLink(null, rowNode)).toBe(false);
  });
});

describe("the favourite a Shift+click landed on", () => {
  const pins = [{ id: "b1", url: "/explorer/view/a" }];
  it("a Favourites row's id; a rail icon by its card's key, else by its address", () => {
    const row = el([".bookmark-row[data-id]"], null, { "data-id": "b9" });
    expect(shiftClickBookmark(dom(el(["a[href]"], row, { href: "/explorer/view/z" })), pins)).toEqual({ id: "b9", url: "/explorer/view/z" });
    const door = el(['[data-left-door^="pin:"]', ".sidebar-rail-btn"], null, { href: "/explorer/view/a", "data-left-door": "pin:b7" });
    expect(shiftClickBookmark(dom(door), pins)).toEqual({ id: "b7", url: "/explorer/view/a" });
    const icon = el([".sidebar-rail-btn"], null, { href: "/explorer/view/a" });
    expect(shiftClickBookmark(dom(icon), pins)).toEqual({ id: "b1", url: "/explorer/view/a" });
  });
  it("nothing for another entry, or an address off this server", () => {
    expect(shiftClickBookmark(dom(el([".sidebar-rail-btn"], null, { href: "/marketplace" })), pins)).toBeNull();
    expect(shiftClickBookmark(dom(el(["a"], null, { href: "/explorer/view/a" })), pins)).toBeNull();
    const row = el([".bookmark-row[data-id]"], null, { "data-id": "b9" });
    expect(shiftClickBookmark(dom(el(["a[href]"], row, { href: "https://x.org" })), pins)).toBeNull();
  });
});

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

describe("the doors in a host", () => {
  let clock: Clock;
  const offs: (() => void)[] = [];
  const item = (key: string, extra: Partial<LeftPeekItem> = {}): LeftPeekItem => ({ key, title: key, icon: null, show: { kind: "frame", src: "/x/" + key }, onOpen: () => {}, ...extra });
  const state = (key: string) => leftCardStore(key).state();
  const enter = (key: string) => leftDoorFor(key)!.onPointerEnter!({ pointerType: "mouse", buttons: 0 } as unknown as ReactPointerEvent<HTMLElement>);
  beforeEach(() => {
    clock = new Clock();
    resetLeftPeekForTests(clock);
  });
  afterEach(() => {
    for (const off of offs.splice(0)) off();
  });

  it("a favourite's plain click shows its card temp and takes the click; a modified one does not", () => {
    publishLeftItems([item("pin:b1", { clickShowsCard: true })]);
    offs.push(leftCardStore("pin:b1").registerCard());
    expect(leftDoorFor("pin:b1")!.onActivate!({ keyboard: false, modified: true })).toBe(false);
    expect(state("pin:b1").up).toBe(false);
    expect(leftDoorFor("pin:b1")!.onActivate!({ keyboard: false, modified: false })).toBe(true);
    expect(state("pin:b1")).toMatchObject({ up: true, pinned: true, held: false });
  });
  it("…its press on the link keeps a card that is up (no blink); a press elsewhere on the row puts it down", () => {
    publishLeftItems([item("pin:b1", { clickShowsCard: true })]);
    offs.push(leftCardStore("pin:b1").registerCard());
    enter("pin:b1");
    clock.tick(PEEK_OPEN_MS);
    const row = el(["row"]);
    const press = (t: El) =>
      leftDoorFor("pin:b1", "row")!.onPointerDown!({ button: 0, target: dom(t), currentTarget: dom(row) } as unknown as ReactPointerEvent<HTMLElement>);
    press(el(["a[href]"], row));
    expect(state("pin:b1").up).toBe(true);
    press(el(["button"], row));
    expect(state("pin:b1").up).toBe(false);
  });
  it("with no card that can stand, the click is the entry's own", () => {
    publishLeftItems([item("pin:b1", { clickShowsCard: true })]);
    expect(leftDoorFor("pin:b1")!.onActivate!({ keyboard: false, modified: false })).toBe(false);
  });
  it("an entry the active tab shows: no card on hover or ArrowRight, and its tooltip stays", () => {
    publishLeftItems([item("ai-models", { active: true })]);
    offs.push(leftCardStore("ai-models").registerCard());
    const door = leftDoorFor("ai-models")!;
    expect(door.quietTip).toBe(false);
    expect(door.attrs?.["aria-haspopup"]).toBeUndefined();
    enter("ai-models");
    clock.tick(PEEK_OPEN_MS * 2);
    expect(state("ai-models").up).toBe(false);
    door.onKeyDown!({ key: "ArrowRight", target: null, altKey: false, ctrlKey: false, metaKey: false, shiftKey: false, preventDefault() {} } as unknown as ReactKeyboardEvent<HTMLElement>);
    expect(state("ai-models").up).toBe(false);
  });
});
