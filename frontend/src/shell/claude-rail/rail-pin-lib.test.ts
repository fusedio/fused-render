// HOVER / TEMP / PIN (rail-pin-lib.ts; owner, 2026-11-16): the parts that need
// no DOM — the transition table row by row, the ring click's reading (a
// double-click is the click's `detail`), the outside press's classifier, where
// a pinned task card lands, and the store halves (door cards, the bot card,
// the hop carry) that hold the two words.
import { describe, expect, test } from "bun:test";
import {
  BRIDGE_SELECTOR,
  cardMode,
  cardStep,
  doorSelector,
  frameBlurCloses,
  hopDropsOpen,
  hopKeeps,
  outsideCloses,
  pinToggle,
  pinWords,
  pinnedTaskLands,
  aPinWord,
  readAPin,
  pressVerdict,
  reloadDropsOpen,
  ringClick,
  ringSelector,
  botRingSelector,
  type CardMode,
} from "./rail-pin-lib";
import { createDoorCardStore } from "./door-card-store";
import {
  botCardIntent,
  botCardState,
  resetBotCardForTests,
  setBotCardHeld,
  setBotCardPinModel,
} from "./bot-card-store";
import { aPinned, botOpen, setAPinned, setBotOpen } from "@platform/lib/last-chat";
import type { Timers } from "./rail-peek-lib";

const MODES: readonly CardMode[] = ["closed", "hover", "temp", "pinned"];

describe("cardMode: the two words spell the four states", () => {
  test("closed / hover / temp / pinned", () => {
    expect(cardMode({ up: false, pinned: true, held: true })).toBe("closed");
    expect(cardMode({ up: true, pinned: false, held: false })).toBe("hover");
    expect(cardMode({ up: true, pinned: true, held: false })).toBe("temp");
    expect(cardMode({ up: true, pinned: true, held: true })).toBe("pinned");
  });
});

describe("the transition table (design.md), row by row", () => {
  test("closed + hover ring/door → hover", () => expect(cardStep("closed", "hover")).toBe("hover"));
  test("hover + pointer leaves → closed", () => expect(cardStep("hover", "leave")).toBe("closed"));
  test("hover + click ring → temp", () => expect(cardStep("hover", "click-ring")).toBe("temp"));
  test("hover + any press inside the card → temp", () => expect(cardStep("hover", "press-inside")).toBe("temp"));
  test("hover / temp + pin icon → pinned", () => {
    expect(cardStep("hover", "pin-icon")).toBe("pinned");
    expect(cardStep("temp", "pin-icon")).toBe("pinned");
  });
  test("closed / hover / temp + double-click ring → pinned", () => {
    for (const m of ["closed", "hover", "temp"] as const) expect(cardStep(m, "dblclick-ring")).toBe("pinned");
    // …and it is the pin's toggle: a pinned window double-clicked is temp.
    expect(cardStep("pinned", "dblclick-ring")).toBe("temp");
  });
  test("temp + press inside → temp (no change)", () => expect(cardStep("temp", "press-inside")).toBe("temp"));
  test("temp + one press outside → closed", () => expect(cardStep("temp", "press-outside")).toBe("closed"));
  test("temp + click own ring → closed; pinned + click own ring → pinned (only the unpin lets it go)", () => {
    expect(cardStep("temp", "click-ring")).toBe("closed");
    expect(cardStep("pinned", "click-ring")).toBe("pinned");
  });
  test("pinned + press outside → pinned (no change)", () => expect(cardStep("pinned", "press-outside")).toBe("pinned"));
  test("pinned + pin icon → temp", () => expect(cardStep("pinned", "pin-icon")).toBe("temp"));
  test("any + × → closed", () => {
    for (const m of MODES) expect(cardStep(m, "close")).toBe("closed");
  });
  test("any + Open ↗ → closed", () => {
    for (const m of MODES) expect(cardStep(m, "open")).toBe("closed");
  });
  test("leaving never closes a card a click made; a hover's leave never pins", () => {
    expect(cardStep("temp", "leave")).toBe("temp");
    expect(cardStep("pinned", "leave")).toBe("pinned");
    expect(cardStep("hover", "hover")).toBe("hover");
    expect(cardStep("pinned", "hover")).toBe("pinned");
  });
  test("a hover's press outside closes nothing the table names; pinned ignores inside presses", () => {
    expect(cardStep("pinned", "press-inside")).toBe("pinned");
    expect(cardStep("closed", "press-outside")).toBe("closed");
  });
  test("rows 13–15: a hop keeps only a pinned card", () => {
    expect(cardStep("hover", "hop")).toBe("closed");
    expect(cardStep("temp", "hop")).toBe("closed");
    expect(cardStep("pinned", "hop")).toBe("pinned");
    expect(hopKeeps("pinned")).toBe(true);
    expect(hopKeeps("temp")).toBe(false);
  });
});

describe("ringClick: the click's own count reads the double-click", () => {
  test("a single click on a closed card's ring, or another ring, opens temp (rows 1–3)", () => {
    expect(ringClick({ mode: "closed", own: false, detail: 1 })).toBe("open");
    expect(ringClick({ mode: "temp", own: false, detail: 1 })).toBe("open");
  });
  test("THE PIN IS THE WINDOW'S: a click that opens or swaps a card in a pinned window opens it pinned", () => {
    expect(ringClick({ mode: "closed", own: false, detail: 1, windowPinned: true })).toBe("pin");
    expect(ringClick({ mode: "closed", own: false, detail: 0, windowPinned: true })).toBe("pin");
    expect(ringClick({ mode: "closed", own: false, detail: 1, windowPinned: false })).toBe("open");
  });
  test("the hover card's own ring: the click makes it temp", () => {
    expect(ringClick({ mode: "hover", own: true, detail: 1 })).toBe("open");
  });
  test("a single click on its own ring closes a TEMP card; a pinned window ignores it", () => {
    expect(ringClick({ mode: "temp", own: true, detail: 1 })).toBe("close");
    expect(ringClick({ mode: "pinned", own: true, detail: 1 })).toBe("keep");
    expect(ringClick({ mode: "pinned", own: true, detail: 1, windowPinned: true })).toBe("keep");
    expect(ringClick({ mode: "pinned", own: true, detail: 0, windowPinned: true })).toBe("keep");
  });
  test("the second click of a double-click pins, never closes — and on the ring a pinned window shows, it unpins", () => {
    for (const mode of MODES) {
      if (mode !== "pinned") expect(ringClick({ mode, own: true, detail: 2 })).toBe("pin");
      expect(ringClick({ mode, own: false, detail: 2 })).toBe("pin");
    }
    expect(ringClick({ mode: "pinned", own: true, detail: 2 })).toBe("unpin");
    expect(ringClick({ mode: "pinned", own: true, detail: 2, windowPinned: true })).toBe("unpin");
    // A third click of the burst changes nothing more.
    expect(ringClick({ mode: "pinned", own: true, detail: 3 })).toBe("keep");
    expect(ringClick({ mode: "temp", own: true, detail: 3 })).toBe("keep");
  });
  test("a pinned window double-clicked walked: the first click keeps, the second unpins", () => {
    expect(ringClick({ mode: "pinned", own: true, detail: 1, windowPinned: true })).toBe("keep");
    expect(ringClick({ mode: "pinned", own: true, detail: 2, windowPinned: true })).toBe("unpin");
  });
  test("row 8 walked: an open temp card double-clicked — closes, then opens pinned (one blink)", () => {
    let mode: CardMode = "temp";
    const first = ringClick({ mode, own: true, detail: 1 });
    expect(first).toBe("close");
    mode = "closed";
    expect(ringClick({ mode, own: false, detail: 2 })).toBe("pin");
  });
  test("row 9: a slow second click (the platform says detail 1) on its own ring closes", () => {
    expect(ringClick({ mode: "temp", own: true, detail: 1 })).toBe("close");
  });
  test("row 20: Enter on a ring (detail 0) is a click", () => {
    expect(ringClick({ mode: "closed", own: false, detail: 0 })).toBe("open");
    expect(ringClick({ mode: "temp", own: true, detail: 0 })).toBe("close");
  });
});

describe("pinToggle, the frame's blur, the pin's words", () => {
  test("hover / temp → pin, pinned → unpin, closed → nothing", () => {
    expect(pinToggle("hover")).toBe("pin");
    expect(pinToggle("temp")).toBe("pin");
    expect(pinToggle("pinned")).toBe("unpin");
    expect(pinToggle("closed")).toBeNull();
  });
  test("a press into a frame closes a temp card only", () => {
    expect(frameBlurCloses("temp")).toBe(true);
    expect(frameBlurCloses("pinned")).toBe(false);
    expect(frameBlurCloses("hover")).toBe(false);
  });
  test("the hint says the double-click; pinned, it says Unpin", () => {
    expect(pinWords(false)).toBe("Pin (double-click the ring)");
    expect(pinWords(true)).toBe("Unpin (double-click the ring)");
  });
});

/** An element stand-in that matches the selectors given. */
function at(...matches: string[]) {
  return { closest: (s: string) => (matches.includes(s) ? {} : null) };
}

describe("pressVerdict: what an outside press is (design.md 'Outside')", () => {
  const card = "#claude-rail-tasks-peek";
  const own = doorSelector("tasks");
  const o = { pressedInside: false, cardSelectors: [card], ownSelectors: [own] };
  test("the card itself, its bridge: inside", () => {
    expect(pressVerdict(at(card), o)).toBe("inside");
    expect(pressVerdict(at(BRIDGE_SELECTOR), o)).toBe("inside");
  });
  test("a menu or picker the card opened (a portal: React's capture says so): inside", () => {
    expect(pressVerdict(at(), { ...o, pressedInside: true })).toBe("inside");
  });
  test("its own door: own — the click is the toggle", () => {
    expect(pressVerdict(at(own), o)).toBe("own");
  });
  test("the page, the rail's blank, a pane, nothing at all: outside", () => {
    expect(pressVerdict(at(), o)).toBe("outside");
    expect(pressVerdict(null, o)).toBe("outside");
  });
  test("the OTHER window's card and rings count as outside (owner's ruling)", () => {
    expect(pressVerdict(at("#claude-rail-fav-peek"), o)).toBe("outside");
    expect(pressVerdict(at(ringSelector(".claude-rail-favs", "f")), o)).toBe("outside");
    expect(pressVerdict(at(botRingSelector("b1")), o)).toBe("outside");
  });
  test("outsideCloses: only a temp card, only an outside press", () => {
    expect(outsideCloses("temp", "outside")).toBe(true);
    expect(outsideCloses("temp", "own")).toBe(false);
    expect(outsideCloses("temp", "inside")).toBe(false);
    expect(outsideCloses("pinned", "outside")).toBe(false);
    expect(outsideCloses("hover", "outside")).toBe(false);
  });
  test("selectors quote a key", () => {
    expect(ringSelector(".claude-rail-chats", 'a"b')).toBe('.claude-rail-chats [data-task-key="a\\"b"]');
    expect(doorSelector("plus")).toBe('[data-rail-door="plus"]');
  });
});

describe("pinnedTaskLands: a pinned window A after a change of view", () => {
  test("its ring is in the list: it stays on the ring", () => {
    expect(pinnedTaskLands({ inList: true })).toBe("ring");
  });
  test("no ring in the next view: the window closes (not the +)", () => {
    expect(pinnedTaskLands({ inList: false })).toBe("close");
  });
});

describe("window A's mark (aPinWord / readAPin)", () => {
  test("round-trips every kind", () => {
    for (const p of [{ kind: "task", key: "a:b" }, { kind: "plus" }, { kind: "tasks" }, { kind: "bots" }, { kind: "git" }] as const)
      expect(readAPin(aPinWord(p))).toEqual(p);
  });
  test("nothing, or a word it does not know, is no pin", () => {
    expect(readAPin(null)).toBeNull();
    expect(readAPin("")).toBeNull();
    expect(readAPin("task:")).toBeNull();
    expect(readAPin("nonsense")).toBeNull();
  });
});

describe("window B's reload and hop rules", () => {
  test("row 19: a reload keeps a pinned open card and drops a temp one", () => {
    const loadedAt = 1_000;
    expect(reloadDropsOpen({ openKey: "f", pinnedKey: "f", openedAt: 10, loadedAt })).toBe(false);
    expect(reloadDropsOpen({ openKey: "f", pinnedKey: null, openedAt: 10, loadedAt })).toBe(true);
    expect(reloadDropsOpen({ openKey: "f", pinnedKey: "g", openedAt: null, loadedAt })).toBe(true);
    // Opened by this document since it loaded: not a reload's.
    expect(reloadDropsOpen({ openKey: "f", pinnedKey: null, openedAt: 2_000, loadedAt })).toBe(false);
    expect(reloadDropsOpen({ openKey: null, pinnedKey: null, openedAt: null, loadedAt })).toBe(false);
  });
  test("rows 13/15: a path change drops the temp card that was open before it; pinned stays", () => {
    const was = { path: "/a", key: "f" };
    expect(hopDropsOpen({ was, now: { path: "/b", key: "f" }, pinnedKey: null })).toBe(true);
    expect(hopDropsOpen({ was, now: { path: "/b", key: "f" }, pinnedKey: "f" })).toBe(false);
    // Same path (a view switch, a node selected): no hop.
    expect(hopDropsOpen({ was, now: { path: "/a", key: "f" }, pinnedKey: null })).toBe(false);
    // Opened BY the hop (the where-chip's hand-over): new, stays.
    expect(hopDropsOpen({ was: { path: "/a", key: null }, now: { path: "/b", key: "f" }, pinnedKey: null })).toBe(false);
  });
});

/** Timers nothing ever fires: these tests read what a press or a hold did at
 *  once, never a hover's delay. */
function manualTimers(): Timers {
  let n = 0;
  return { set: () => ++n, clear: () => {} };
}

describe("door-card-store: the hold", () => {
  test("a press is temp; the pin holds; the pin is dropped with the card; only an up card holds", () => {
    const store = createDoorCardStore("k", "id");
    store.resetForTests(manualTimers());
    store.setHeld(true);
    expect(store.state().held).toBe(false);
    store.intent().press("k");
    expect(store.state()).toMatchObject({ up: true, pinned: true, held: false });
    store.setHeld(true);
    expect(store.state().held).toBe(true);
    store.setHeld(false);
    expect(store.state()).toMatchObject({ up: true, pinned: true, held: false });
    store.setHeld(true);
    store.close();
    expect(store.state()).toMatchObject({ up: false, pinned: false, held: false });
  });
  test("its press flag is the card's to raise and a reset's to lower", () => {
    const store = createDoorCardStore("k", "id");
    store.press.inside = true;
    store.resetForTests(manualTimers());
    expect(store.press.inside).toBe(false);
  });
});

describe("door-card-store: window A's mark follows the hold", () => {
  test("held writes the card's word; the unpin and a close let go only a mark that still names it", () => {
    setAPinned(null);
    const store = createDoorCardStore("k", "id", "tasks");
    store.resetForTests(manualTimers());
    store.intent().press("k");
    expect(aPinned()).toBeNull();
    store.setHeld(true);
    expect(aPinned()).toBe("tasks");
    store.setHeld(false);
    expect(aPinned()).toBeNull();
    store.setHeld(true);
    // Another card took the window meanwhile: its mark is not this one's to clear.
    setAPinned("plus");
    store.close();
    expect(aPinned()).toBe("plus");
    setAPinned(null);
    store.resetForTests();
  });
});

describe("bot-card-store with the pin model", () => {
  test("a press is temp; the hold is the pin's; a swap to another bot is temp; the window's mark follows the hold only", () => {
    resetBotCardForTests(manualTimers());
    setBotOpen(null);
    setBotCardPinModel(true);
    botCardIntent().press("b1");
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: false });
    // Row 19: a temp card is not what a reload brings back.
    expect(botOpen()).toBeNull();
    setBotCardHeld(true);
    expect(botCardState().held).toBe(true);
    expect(botOpen()).toBe("b1");
    // Row 2 in window B: another bot's card swaps in temp.
    botCardIntent().press("b2");
    expect(botCardState()).toMatchObject({ key: "b2", pinned: true, held: false });
    expect(botOpen()).toBeNull();
    setBotCardHeld(true);
    botCardIntent().dismiss();
    expect(botCardState()).toMatchObject({ key: null, pinned: false, held: false });
    expect(botOpen()).toBeNull();
    resetBotCardForTests();
  });
  test("flag off: a press holds where the rail says (favourites on), and marks the window, as before", () => {
    resetBotCardForTests(manualTimers());
    setBotOpen(null);
    botCardIntent().press("b1");
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: true });
    expect(botOpen()).toBe("b1");
    botCardIntent().dismiss();
    expect(botOpen()).toBeNull();
    resetBotCardForTests();
  });
});
