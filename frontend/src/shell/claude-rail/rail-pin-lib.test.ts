// HOVER / TEMP / PIN (rail-pin-lib.ts; owner, 2026-11-16): the parts that need
// no DOM — the transition table row by row, the ring click's reading (it
// opens, never closes, and no double-click pins: 2026-11-17), the outside
// press's classifier (the rail and its windows are the view), where
// a pinned task card lands, and the store halves (door cards, the bot card,
// the hop carry) that hold the two words.
import { describe, expect, test } from "bun:test";
import {
  BRIDGE_SELECTOR,
  LEFT_CARD_SELECTOR,
  RAIL_BRIDGE_SELECTOR,
  cardMode,
  cardStep,
  doorSelector,
  frameBlurCloses,
  hopDropsOpen,
  hopKeeps,
  inFullModal,
  FULL_MODAL_SELECTOR,
  inRailView,
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
  RAIL_VIEW_SELECTORS,
  type CardMode,
} from "./rail-pin-lib";
import { createDoorCardStore } from "./door-card-store";
import {
  botCardIntent,
  botCardState,
  resetBotCardForTests,
  setBotCardHeld,
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
  test("temp + press inside → temp (no change)", () => expect(cardStep("temp", "press-inside")).toBe("temp"));
  test("temp + one press outside → closed", () => expect(cardStep("temp", "press-outside")).toBe("closed"));
  test("an open card + click own ring → no change: a ring opens, it is not a toggle (2026-11-17)", () => {
    expect(cardStep("temp", "click-ring")).toBe("temp");
    expect(cardStep("pinned", "click-ring")).toBe("pinned");
    expect(cardStep("closed", "click-ring")).toBe("temp");
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

describe("ringClick: every rail icon toggles its temp card closed; a pinned card ignores its own icon; no double-click pins", () => {
  test("a click on a closed card's ring, or another ring, opens temp (rows 1–3)", () => {
    expect(ringClick({ mode: "closed", own: false })).toBe("open");
    expect(ringClick({ mode: "temp", own: false })).toBe("open");
  });
  test("THE PIN IS THE WINDOW'S: a click that opens or swaps a card in a pinned window opens it pinned", () => {
    expect(ringClick({ mode: "closed", own: false, windowPinned: true })).toBe("pin");
    expect(ringClick({ mode: "closed", own: false, windowPinned: false })).toBe("open");
  });
  test("the hover card's own ring: the click makes it temp", () => {
    expect(ringClick({ mode: "hover", own: true })).toBe("open");
  });
  test("the icon of a TEMP card closes it (owner, 2026-11-19); a pinned card's icon does nothing", () => {
    expect(ringClick({ mode: "temp", own: true })).toBe("close");
    expect(ringClick({ mode: "temp", own: true, windowPinned: true })).toBe("close");
    expect(ringClick({ mode: "pinned", own: true })).toBe("keep");
    expect(ringClick({ mode: "pinned", own: true, windowPinned: true })).toBe("keep");
  });
  test("it never unpins: only a temp card's own icon closes, whatever the window", () => {
    const acts = new Set<string>();
    for (const mode of MODES) for (const own of [true, false]) for (const windowPinned of [true, false]) acts.add(ringClick({ mode, own, windowPinned }));
    expect([...acts].sort()).toEqual(["close", "keep", "open", "pin"]);
    expect(ringClick({ mode: "pinned", own: true })).not.toBe("close");
  });
  test("a double-click walked: the first click opens temp, the second lands on the open card and closes it", () => {
    expect(ringClick({ mode: "closed", own: false })).toBe("open");
    expect(ringClick({ mode: "temp", own: true })).toBe("close");
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
  test("the hint is the one word — the pin icon is the only pin (2026-11-17)", () => {
    expect(pinWords(false)).toBe("Pin");
    expect(pinWords(true)).toBe("Unpin");
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
  test("its own door: own — its click opens and never closes", () => {
    expect(pressVerdict(at(own), o)).toBe("own");
  });
  test("the page, a pane, the left sidebar, nothing at all: outside", () => {
    expect(pressVerdict(at(), o)).toBe("outside");
    expect(pressVerdict(null, o)).toBe("outside");
    expect(pressVerdict(at(".sidebar"), o)).toBe("outside");
  });
  test("THE RAIL AND THE OTHER WINDOW ARE THE VIEW, NOT OUTSIDE (owner, 2026-11-17: 'rail and window a/b clicks don't count')", () => {
    // The rail's blank and any ring or door on it (all inside `.claude-rail`)…
    expect(pressVerdict(at(".claude-rail"), o)).toBe("view");
    expect(pressVerdict(at(".claude-rail", ringSelector(".claude-rail-favs", "f")), o)).toBe("view");
    expect(pressVerdict(at(".claude-rail", botRingSelector("b1")), o)).toBe("view");
    // …the other window's card (every card stands in `.claude-rail-peek`)…
    expect(pressVerdict(at(".claude-rail-peek", "#claude-rail-fav-peek"), o)).toBe("view");
    // …and the + New task popover.
    expect(pressVerdict(at(".modal-popover"), o)).toBe("view");
    expect(RAIL_VIEW_SELECTORS).toEqual([".claude-rail", ".claude-rail-peek", ".modal-popover", BRIDGE_SELECTOR]);
    expect(inRailView(at(".claude-rail"))).toBe(true);
    expect(inRailView(at())).toBe(false);
    expect(inRailView(null)).toBe(false);
  });
  test("THE LEFT SIDEBAR'S CARDS ARE NOT THIS RAIL'S VIEW (shell/left-rail): a press in one is outside", () => {
    expect(pressVerdict(at(".claude-rail-peek", LEFT_CARD_SELECTOR), o)).toBe("outside");
    expect(pressVerdict(at(BRIDGE_SELECTOR, LEFT_CARD_SELECTOR), o)).toBe("outside");
    expect(inRailView(at(".claude-rail-peek", LEFT_CARD_SELECTOR))).toBe(false);
    // …while the card's own boxes still come first.
    expect(pressVerdict(at(card, LEFT_CARD_SELECTOR), o)).toBe("inside");
    expect(RAIL_BRIDGE_SELECTOR).toBe(".claude-rail-peek-bridge:not(.is-left)");
  });
  test("a frame in a full modal (a chat's file preview) is the modal's, not the page's frame", () => {
    expect(inFullModal(at(FULL_MODAL_SELECTOR))).toBe(true);
    expect(inFullModal(at(".modal-popover"))).toBe(false);
    expect(inFullModal(at())).toBe(false);
    expect(inFullModal(null)).toBe(false);
  });
  test("outsideCloses: only a temp card, only an outside press", () => {
    expect(outsideCloses("temp", "outside")).toBe(true);
    expect(outsideCloses("temp", "view")).toBe(false);
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
});
