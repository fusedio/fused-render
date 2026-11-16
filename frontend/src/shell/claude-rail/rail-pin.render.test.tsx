// RAIL CARDS: HOVER / TEMP / PIN, MOUNTED (rail-pin-lib.ts; owner, 2026-11-16).
// Every door × the transitions that need the rail: a click is temp, one press
// outside closes it, the pin icon (or a double-click) pins and outside presses
// are ignored, the pin icon again is temp, the own ring closes; window A and B
// close each other's temp card; the hop rows 13–17; and the switch off keeps
// every rule it had. Real modules throughout, as ClaudeRail.render.test.tsx;
// the document's and the window's listeners are recorded so a press, a key
// and a frame's blur can be played to them.
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { ReactElement } from "react";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import type { RailPlace } from "./claude-rail-lib";

const { ClaudeRail } = await import("./ClaudeRail");
const { RailPeek } = await import("./RailPeek");
const { BotPeek } = await import("./BotPeek");
const { BotCardHost } = await import("./BotCardHost");
const { DoorCard } = await import("./DoorCard");
const { TaskHead } = await import("@shell/TaskHead");
const { publishTaskFavourites, publishFavouritesParent, resetTaskFavouritesFlagForTests } = await import(
  "@platform/lib/favourites-flag"
);
const { publishCanvasRailPeek, resetCanvasRailPeekFlagForTests } = await import("./rail-peek-flag");
const { publishTasks, rememberListing, forgetListing, resetListingFeedForTests } = await import("@shell/tasksPulse");
const { favPeekState, resetFavPeekStoreForTests, resetFavHoverForTests } = await import("./fav-peek-store");
const { publishBots, resetBotsPulseForTests } = await import("@platform/lib/botsPulse");
const { setBotsAvailableForTests } = await import("@platform/lib/bots-available");
const { aPinned, botOpen, favOpen, favPinned, setAPinned, setBotOpen, setFavOpen, setFavPinned } = await import("@platform/lib/last-chat");
const { dropSharedPrefsRead } = await import("@platform/lib/api");
const { botCardState, resetBotCardForTests } = await import("./bot-card-store");
const { botsCardState, registerBotsCard, resetBotsCardForTests } = await import("./bots-peek-store");
const { registerTasksCard, resetTasksCardForTests, tasksCardState, tasksCardStore } = await import("./tasks-peek-store");
const { forgetGitCardRail, gitCardIntent, gitCardState, gitCardStore, resetGitCardForTests, setGitCardRail, GIT_CARD_KEY } =
  await import("./git-peek-store");
const { publishTaskHeadUnified, resetTaskHeadUnifiedFlagForTests } = await import("@shell/task-head-flag");
const { resetFlagsForTests: resetClaudeFlagsForTests } = await import("@apps/claude/feature-flag");
const { getPlusCard, plusHostElement, registerPlusHost, resetPlusCardStoreForTests } = await import("./plus-card-store");
const { doorSelector, ringSelector, botRingSelector } = await import("./rail-pin-lib");
const { PLUS_DOOR } = await import("./plus-pop-lib");

const DIR = "/home/me/apps/demo";
const OTHER = "/home/me/apps/other";

function chat(key: string, over: Partial<Task> = {}): Task {
  return {
    task_id: key,
    key,
    session_id: `s-${key}`,
    status: "done",
    unread: 0,
    last_active: 1_000,
    happened_at: 1_000,
    project: DIR,
    target: DIR,
    title: key,
    ...over,
  } as Task;
}

function rect(left: number, top: number, width: number, height: number) {
  return { left, top, right: left + width, bottom: top + height, width, height, x: left, y: top };
}
const ROW = { getBoundingClientRect: () => rect(0, 0, 1400, 900), parentElement: null };
function fakeEl(): Record<string, unknown> {
  const el: Record<string, unknown> = {
    nodeType: 1,
    hidden: false,
    style: { setProperty() {}, removeProperty() {} },
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    getBoundingClientRect: () => rect(1352, 100, 48, 32),
    getClientRects: () => [],
    addEventListener() {},
    removeEventListener() {},
    setAttribute() {},
    removeAttribute() {},
    toggleAttribute() {},
    getAttribute: () => null,
    hasAttribute: () => false,
    querySelector: () => null,
    querySelectorAll: () => [],
    closest: () => null,
    contains: () => false,
    matches: () => false,
    focus() {},
    blur() {},
    scrollIntoView() {},
    clientHeight: 600,
    clientWidth: 48,
    offsetHeight: 600,
    offsetWidth: 48,
    scrollHeight: 600,
    scrollTop: 0,
    parentElement: ROW,
    firstElementChild: null,
    children: [],
  };
  el.ownerDocument = (globalThis as { document?: unknown }).document;
  return el;
}

const g = globalThis as Record<string, unknown>;
const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
const win = (globalThis as unknown as { window: Record<string, unknown> }).window;
const loc = (globalThis as unknown as { location: { pathname: string } }).location;
const realFetch = globalThis.fetch;
const MISSING = Symbol("missing");
const saved = new Map<string, unknown>();
const measuring: Record<string, unknown> = {
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  MutationObserver: class {
    observe() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
  },
  IntersectionObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  getComputedStyle: () => ({ display: "block", paddingTop: "0", paddingBottom: "0", rowGap: "0", getPropertyValue: () => "" }),
};

// THE DOCUMENT'S AND THE WINDOW'S LISTENERS, recorded (the shim's are no-ops).
type Fn = (e: unknown) => void;
const docL = new Map<string, Set<Fn>>();
const winL = new Map<string, Set<Fn>>();
const savedListen = new Map<string, unknown>();
function record(target: Record<string, unknown>, into: Map<string, Set<Fn>>, name: string) {
  savedListen.set(name + ".add", target.addEventListener);
  savedListen.set(name + ".remove", target.removeEventListener);
  target.addEventListener = (type: string, fn: Fn) => {
    if (!into.has(type)) into.set(type, new Set());
    into.get(type)!.add(fn);
  };
  target.removeEventListener = (type: string, fn: Fn) => {
    into.get(type)?.delete(fn);
  };
}
let hadGetById = false;
let hadActive: unknown = null;
let hadRoot: unknown = MISSING;

beforeEach(() => {
  for (const [name, stub] of Object.entries(measuring)) {
    saved.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
  record(doc, docL, "doc");
  record(win, winL, "win");
  installPortalContainer();
  (doc.body as { createNodeMock: unknown }).createNodeMock = () => fakeEl();
  hadGetById = "getElementById" in doc;
  doc.getElementById ??= () => null;
  hadActive = doc.activeElement;
  // The + card's store marks the root while a card waits for a rail
  // (plus-card-store `plus-card-orphan`); the shim's document has none.
  hadRoot = "documentElement" in doc ? doc.documentElement : MISSING;
  doc.documentElement ??= {
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    style: { setProperty() {}, removeProperty() {} },
  };
  resetTaskFavouritesFlagForTests();
  resetCanvasRailPeekFlagForTests();
  resetTaskHeadUnifiedFlagForTests();
  resetFavPeekStoreForTests();
  resetFavHoverForTests();
  resetListingFeedForTests();
  forgetListing();
  resetBotCardForTests();
  resetBotsPulseForTests();
  resetTasksCardForTests();
  resetBotsCardForTests();
  resetGitCardForTests();
  resetPlusCardStoreForTests();
  setAPinned(null);
  setFavOpen(null);
  setFavPinned(null);
  setBotOpen(null);
  (globalThis as { fetch: unknown }).fetch = () => new Promise<Response>(() => {});
  setBotsAvailableForTests(true);
});

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  publishTasks([]);
  resetListingFeedForTests();
  forgetListing();
  resetFavPeekStoreForTests();
  resetTaskFavouritesFlagForTests();
  resetCanvasRailPeekFlagForTests();
  resetBotCardForTests();
  resetBotsPulseForTests();
  resetTasksCardForTests();
  resetBotsCardForTests();
  resetGitCardForTests();
  resetPlusCardStoreForTests();
  setAPinned(null);
  setFavOpen(null);
  setFavPinned(null);
  setBotOpen(null);
  setBotsAvailableForTests(null);
  (globalThis as { fetch: unknown }).fetch = realFetch;
  dropSharedPrefsRead();
  resetClaudeFlagsForTests();
  resetTaskHeadUnifiedFlagForTests();
  removePortalContainer();
  if (!hadGetById) delete doc.getElementById;
  doc.activeElement = hadActive;
  if (hadRoot === MISSING) delete doc.documentElement;
  doc.addEventListener = savedListen.get("doc.add");
  doc.removeEventListener = savedListen.get("doc.remove");
  win.addEventListener = savedListen.get("win.add");
  win.removeEventListener = savedListen.get("win.remove");
  docL.clear();
  winL.clear();
  loc.pathname = "/";
  for (const [name, was] of saved) {
    if (was === MISSING) delete g[name];
    else g[name] = was;
  }
  saved.clear();
});

const tick = (ms = 5) => new Promise((res) => setTimeout(res, ms));
const settle = () =>
  act(async () => {
    await tick();
  });

function Rail({ place, unified, ...rest }: { place?: RailPlace; unified?: boolean; railKey?: string }) {
  void unified;
  return (
    <ClaudeRail
      key={rest.railKey ?? "rail"}
      paneOpen={false}
      place={place ?? { kind: "folder", dir: DIR }}
      newTask={{ target: DIR, waiting: null, on: null }}
      activeSessionId={null}
      onOpen={() => {}}
      onOpenChat={() => {}}
      onPullStart={() => {}}
    />
  );
}

async function mount(o: {
  unified: boolean;
  favourites?: boolean;
  chats?: Task[];
  extra?: ReactElement | null;
  bots?: boolean;
}): Promise<ReactTestRenderer> {
  publishCanvasRailPeek({ enabled: true });
  publishFavouritesParent({ enabled: true });
  publishTaskFavourites({ enabled: o.favourites ?? true });
  if (o.unified) publishTaskHeadUnified({ enabled: true });
  const rows = o.chats ?? [chat("a"), chat("b")];
  rememberListing(rows);
  publishTasks(rows as unknown as TaskPulseTask[]);
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      <>
        <Rail />
        {o.bots !== false && <BotCardHost />}
        {o.extra ?? null}
      </>,
      { createNodeMock: () => fakeEl() },
    );
  });
  mounted.push(r);
  await settle();
  return r;
}

/** A click event with the platform's count (`detail`: 0 Enter, 1, 2…). */
const click = (detail: number) => ({
  preventDefault() {},
  stopPropagation() {},
  button: 0,
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  altKey: false,
  detail,
  currentTarget: fakeEl(),
});

const rings = (r: ReactTestRenderer, key: string) => r.root.findAll((n) => n.type === "a" && n.props["data-task-key"] === key);
const listRing = (r: ReactTestRenderer, key: string) => rings(r, key)[0]!;
/** The favourites' section draws after the list: its ring is the last. */
const footRing = (r: ReactTestRenderer, key: string) => { const all = rings(r, key); return all[all.length - 1]!; };
const peek = (r: ReactTestRenderer) => r.root.findAllByType(RailPeek)[0] ?? null;
const shownKey = (r: ReactTestRenderer): string | null => peek(r)?.props.shown?.pulse.key ?? null;
const byLabel = (r: ReactTestRenderer, label: string): ReactTestInstance[] =>
  r.root.findAll((n) => typeof n.type === "string" && n.props["aria-label"] === label);

async function press(n: ReactTestInstance, detail = 1) {
  await act(async () => {
    n.props.onClick(click(detail));
    await tick();
  });
}

/** An element the classifier reads, matching the selectors given. */
const at = (...matches: string[]) => ({ closest: (s: string) => (matches.includes(s) ? {} : null) });
const NOWHERE = at();

/** A pointerdown played to the document's capture listeners, then the tick
 *  the watchers wait. */
async function pressAt(target: unknown) {
  await act(async () => {
    for (const fn of [...(docL.get("pointerdown") ?? [])]) fn({ target, button: 0 });
    await tick();
  });
}

async function escapeOnBody() {
  await act(async () => {
    for (const fn of [...(docL.get("keydown") ?? [])])
      fn({ key: "Escape", defaultPrevented: false, target: doc.body, preventDefault() {} });
    await tick();
  });
}

/** A press into the canvas frame: the window's blur with an iframe focused. */
async function frameBlur() {
  const Frame = g.HTMLIFrameElement as new () => object;
  doc.activeElement = new Frame();
  await act(async () => {
    for (const fn of [...(winL.get("blur") ?? [])]) fn({});
    await tick();
  });
  doc.activeElement = hadActive;
}

// ---------------------------------------------------------------------------

describe("the task ring's card (window A), switch on", () => {
  test("a click is TEMP: one press outside closes it, and so does Escape; a PINNED card ignores Escape", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.pinned).toBe(true);
    expect(peek(r)!.props.held).toBe(false);
    expect(peek(r)!.props.pinCtl.pinned).toBe(false);
    await pressAt(NOWHERE);
    expect(shownKey(r)).toBeNull();
    // Escape's one rule (owner, 2026-11-16): temp closes, pinned stays.
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBe("a");
    await escapeOnBody();
    expect(shownKey(r)).toBeNull();
    await press(listRing(r, "a"), 1);
    await press(listRing(r, "a"), 2);
    expect(peek(r)!.props.pinCtl.pinned).toBe(true);
    await escapeOnBody();
    expect(shownKey(r)).toBe("a");
  });

  test("the pin icon PINS (outside presses and the frame ignored), again is temp, then an outside press closes", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    expect(peek(r)!.props.held).toBe(true);
    expect(peek(r)!.props.pinCtl.pinned).toBe(true);
    await pressAt(NOWHERE);
    await frameBlur();
    expect(shownKey(r)).toBe("a");
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    expect(peek(r)!.props.held).toBe(false);
    expect(shownKey(r)).toBe("a");
    await pressAt(NOWHERE);
    expect(shownKey(r)).toBeNull();
  });

  test("a press into the frame closes a temp card", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    await frameBlur();
    expect(shownKey(r)).toBeNull();
  });

  test("its own ring's click closes a TEMP card; a pinned window ignores it — only the unpin lets it go", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBeNull();
    await press(listRing(r, "a"));
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
    expect(aPinned()).toBe("task:" + chat("a").key);
    // Unpinned: temp again, the mark gone, and its ring closes it.
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    expect(peek(r)!.props.held).toBe(false);
    expect(aPinned()).toBeNull();
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBeNull();
  });

  test("a double-click pins (rows 7/8): from closed, and over its own temp card", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 1);
    await press(listRing(r, "a"), 2);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
    // A single click on its own ring: a pinned window stays.
    await press(listRing(r, "a"), 1);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
    // A double-click on it is the unpin: the card stays, temp.
    await press(listRing(r, "a"), 2);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(false);
    // Row 9: temp again, a slow second click (detail 1) on its own ring closes.
    await press(listRing(r, "a"), 1);
    expect(shownKey(r)).toBeNull();
    // A lone second click (the first one closed it): opens pinned.
    await press(listRing(r, "b"), 2);
    expect(shownKey(r)).toBe("b");
    expect(peek(r)!.props.held).toBe(true);
  });

  test("THE PIN IS THE WINDOW'S: another ring of a pinned window swaps what it shows, still pinned", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 2);
    expect(peek(r)!.props.held).toBe(true);
    await press(listRing(r, "b"));
    expect(shownKey(r)).toBe("b");
    expect(peek(r)!.props.held).toBe(true);
    expect(aPinned()).toBe("task:" + chat("b").key);
  });

  test("no × with the pin: the head's first button is the pin", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    const labels = peek(r)!
      .findAll((n) => n.type === "button")
      .map((n) => String(n.props["aria-label"] ?? ""));
    expect(labels).not.toContain("Close the preview");
    expect(labels).toContain("Pin");
  });

  test("row 20: Enter on a ring (detail 0) is a click: temp", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 0);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(false);
  });
});

describe("the switch off: every rule as it was", () => {
  test("favourites on: a click HOLDS at once; an outside press and the frame leave it; no pin icon", async () => {
    const r = await mount({ unified: false });
    await press(listRing(r, "a"));
    expect(peek(r)!.props.held).toBe(true);
    expect(peek(r)!.props.pinCtl).toBeNull();
    await pressAt(NOWHERE);
    await frameBlur();
    expect(shownKey(r)).toBe("a");
    // A double-click is two clicks: the second closes (the toggle).
    await press(listRing(r, "b"), 1);
    await press(listRing(r, "b"), 2);
    expect(shownKey(r)).toBeNull();
  });

  test("favourites off: a clicked card is not held, and Escape with the focus nowhere closes it", async () => {
    const r = await mount({ unified: false, favourites: false });
    await press(listRing(r, "a"));
    expect(peek(r)!.props.held).toBe(false);
    await escapeOnBody();
    expect(shownKey(r)).toBeNull();
  });

  test("the bot ring: a press holds, the window's mark at once; the Tasks door's second press closes", async () => {
    const off = registerTasksCard();
    const r = await mount({ unified: false });
    await act(async () => {
      publishBots([{ id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" }] as never);
    });
    await press(r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1"));
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: true });
    expect(botOpen()).toBe("b1");
    await press(byLabel(r, "Tasks")[0]!, 1);
    await press(byLabel(r, "Tasks")[0]!, 2);
    expect(tasksCardState().up).toBe(false);
    off();
  });
});

describe("the favourite's card (window B), switch on", () => {
  const starred = () => [chat("a"), chat("f", { starred: true })];

  test("a click is temp; one press outside closes it; a double-click pins and outside presses and its own ring are ignored", async () => {
    const r = await mount({ unified: true, chats: starred() });
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
    await pressAt(NOWHERE);
    expect(favOpen()).toBeNull();
    await press(footRing(r, "f"), 1);
    await press(footRing(r, "f"), 2);
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBe("f");
    await pressAt(NOWHERE);
    await frameBlur();
    await escapeOnBody();
    expect(favOpen()).toBe("f");
    // Its own ring: a pinned window stays; a double-click on it unpins
    // (temp), and then the ring closes it.
    await press(footRing(r, "f"), 1);
    expect(favOpen()).toBe("f");
    await press(footRing(r, "f"), 2);
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
    await press(footRing(r, "f"), 1);
    expect(favOpen()).toBeNull();
    // Escape's one rule: with the focus nowhere it closes a temp card.
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    await escapeOnBody();
    expect(favOpen()).toBeNull();
  });

  test("its own ring is not an outside press; the card's own tree (a portal) is inside", async () => {
    const { favPress } = await import("./fav-peek-store");
    const r = await mount({ unified: true, chats: starred() });
    await press(footRing(r, "f"));
    await pressAt(at(ringSelector(".claude-rail-favs", "f")));
    expect(favOpen()).toBe("f");
    await act(async () => {
      for (const fn of [...(docL.get("pointerdown") ?? [])]) fn({ target: NOWHERE, button: 0 });
      // React's capture phase, from a menu the card portaled out.
      favPress.inside = true;
      await tick();
    });
    expect(favOpen()).toBe("f");
  });

  test("a press into the frame closes a temp favourite's card", async () => {
    const r = await mount({ unified: true, chats: starred() });
    await press(footRing(r, "f"));
    await frameBlur();
    expect(favOpen()).toBeNull();
  });
});

describe("window A and window B (rows 3, 4, 6)", () => {
  const starred = () => [chat("a"), chat("f", { starred: true })];

  test("A temp, a press on B's ring: A closes, and B opens temp", async () => {
    const r = await mount({ unified: true, chats: starred() });
    await press(listRing(r, "a"));
    await pressAt(at(ringSelector(".claude-rail-favs", "f")));
    expect(shownKey(r)).toBeNull();
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
  });

  test("B temp, a press on A's ring: B closes", async () => {
    const r = await mount({ unified: true, chats: starred() });
    await press(footRing(r, "f"));
    await pressAt(at(ringSelector(".claude-rail-chats", "a")));
    expect(favOpen()).toBeNull();
  });

  test("row 4: A pinned, B opened: both stay up; B pinned, A opened: both stay", async () => {
    const r = await mount({ unified: true, chats: starred() });
    await press(listRing(r, "a"), 2);
    await pressAt(at(ringSelector(".claude-rail-favs", "f")));
    await press(footRing(r, "f"), 2);
    expect(shownKey(r)).toBe("a");
    expect(favOpen()).toBe("f");
    await pressAt(at(ringSelector(".claude-rail-chats", "b")));
    expect(favOpen()).toBe("f");
  });
});

describe("the bot ring's card (window B), switch on", () => {
  const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
  const ring = (r: ReactTestRenderer) => r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");

  test("temp: no mark for a reload, one press outside or Escape closes; pinned by a double-click: the mark, outside ignored; the pin icon on its head", async () => {
    const r = await mount({ unified: true });
    await act(async () => {
      publishBots([BOT] as never);
    });
    await press(ring(r));
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: false });
    expect(botOpen()).toBeNull();
    expect(r.root.findByType(BotPeek).props.pinCtl.pinned).toBe(false);
    await pressAt(NOWHERE);
    expect(botCardState().key).toBeNull();
    // Escape's one rule: temp closes on it, pinned (below) does not.
    await press(ring(r));
    await escapeOnBody();
    expect(botCardState().key).toBeNull();
    await press(ring(r), 1);
    await press(ring(r), 2);
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    await escapeOnBody();
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    expect(botOpen()).toBe("b1");
    await pressAt(NOWHERE);
    expect(botCardState().key).toBe("b1");
    // The pin icon again: temp, the mark gone.
    await act(async () => r.root.findByType(BotPeek).props.pinCtl.onToggle());
    expect(botCardState().held).toBe(false);
    expect(botOpen()).toBeNull();
    // Its ring: closes.
    await press(ring(r), 1);
    expect(botCardState().key).toBeNull();
  });

  test("row 15 / 13: a page hop keeps a pinned bot card and closes a temp one", async () => {
    const Host = ({ n }: { n: number }) => <BotCardHost key="host" data-n={n} />;
    const r = await mount({ unified: true, bots: false, extra: <Host n={1} /> });
    await act(async () => {
      publishBots([BOT] as never);
    });
    await press(ring(r), 2);
    loc.pathname = "/elsewhere";
    await act(async () => r.update(<><Rail /><Host n={2} /></>));
    await settle();
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    await act(async () => r.root.findByType(BotPeek).props.pinCtl.onToggle());
    loc.pathname = "/third";
    await act(async () => r.update(<><Rail /><Host n={3} /></>));
    await settle();
    expect(botCardState().key).toBeNull();
  });

  test("a reload pins the window's open bot back — pinned", async () => {
    setBotOpen("b1");
    const r = await mount({ unified: true });
    await act(async () => {
      publishBots([BOT] as never);
      await tick();
    });
    void r;
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: true });
  });
});

describe("the Tasks, Bots and Git doors' cards (window A), switch on", () => {
  test("Tasks: a press is temp, one press outside closes it; its own door is not outside; a double-click pins; the frame closes temp only", async () => {
    const off = registerTasksCard();
    const r = await mount({ unified: true });
    const door = () => byLabel(r, "Tasks")[0]!;
    expect(door().props["data-rail-door"]).toBe("tasks");
    await press(door());
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, held: false });
    await pressAt(at(doorSelector("tasks")));
    expect(tasksCardState().up).toBe(true);
    await pressAt(NOWHERE);
    expect(tasksCardState().up).toBe(false);
    await press(door(), 1);
    await frameBlur();
    expect(tasksCardState().up).toBe(false);
    await press(door(), 1);
    await press(door(), 2);
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    await pressAt(NOWHERE);
    await frameBlur();
    // …and Escape with the focus nowhere, which closed a clicked Tasks card
    // before the pin, leaves a pinned one up.
    await escapeOnBody();
    expect(tasksCardState().up).toBe(true);
    expect(aPinned()).toBe("tasks");
    // Its own door's click: a pinned window stays; unpinned, the door closes it.
    await press(door(), 1);
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    // A double-click on its door is the unpin: the card stays, temp.
    await press(door(), 2);
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, held: false });
    expect(aPinned()).toBeNull();
    await press(door(), 1);
    expect(tasksCardState().up).toBe(false);
    off();
  });

  test("Bots: the same — temp closes outside, a double-click pins", async () => {
    const off = registerBotsCard();
    const r = await mount({ unified: true });
    await press(byLabel(r, "Bots")[0]!);
    expect(botsCardState()).toMatchObject({ up: true, pinned: true, held: false });
    await pressAt(NOWHERE);
    expect(botsCardState().up).toBe(false);
    await press(byLabel(r, "Bots")[0]!, 2);
    expect(botsCardState()).toMatchObject({ up: true, held: true });
    await pressAt(NOWHERE);
    expect(botsCardState().up).toBe(true);
    off();
  });

  test("Git: temp closes outside and on its rail going; pinned stays through both", async () => {
    const r = await mount({ unified: true });
    void r;
    await act(async () => {
      gitCardIntent().press(GIT_CARD_KEY);
    });
    expect(gitCardState()).toMatchObject({ up: true, pinned: true, held: false });
    await pressAt(NOWHERE);
    expect(gitCardState().up).toBe(false);
    const railEl = fakeEl() as unknown as HTMLElement;
    await act(async () => {
      setGitCardRail(railEl);
      gitCardIntent().press(GIT_CARD_KEY);
      forgetGitCardRail(railEl);
    });
    expect(gitCardState().up).toBe(false);
    await act(async () => {
      setGitCardRail(railEl);
      gitCardIntent().press(GIT_CARD_KEY);
      gitCardStore.setHeld(true);
    });
    await pressAt(NOWHERE);
    await act(async () => forgetGitCardRail(railEl));
    expect(gitCardState()).toMatchObject({ up: true, held: true });
  });

  test("row 14: a hop keeps a pinned Tasks card and closes a temp one (DoorCard); its head's pin toggles the hold", async () => {
    let api: { pin: { pinned: boolean; onToggle: () => void } | null } | null = null;
    const card = (
      <DoorCard store={tasksCardStore} href="/tasks" className="t" ariaLabel="Tasks" subOpen={false}>
        {(a) => {
          api = a;
          return null;
        }}
      </DoorCard>
    );
    // The card first: it is the shell's, mounted once — a hop swaps the rail
    // beside it, never the card itself.
    publishCanvasRailPeek({ enabled: true });
    publishFavouritesParent({ enabled: true });
    publishTaskFavourites({ enabled: true });
    publishTaskHeadUnified({ enabled: true });
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<>{card}<Rail /></>, { createNodeMock: () => fakeEl() });
    });
    mounted.push(r);
    await settle();
    await press(r.root.find((n) => n.type === "a" && n.props["data-rail-door"] === "tasks"));
    expect(api!.pin).toEqual({ pinned: false, onToggle: expect.any(Function) });
    await act(async () => api!.pin!.onToggle());
    expect(tasksCardState().held).toBe(true);
    await act(async () => r.update(<>{card}<Rail railKey="two" /></>));
    await settle();
    expect(favPeekState().rail).not.toBeNull();
    expect(tasksCardState().up).toBe(true);
    await act(async () => api!.pin!.onToggle());
    expect(tasksCardState()).toMatchObject({ up: true, held: false });
    await act(async () => r.update(<>{card}<Rail railKey="three" /></>));
    await settle();
    expect(tasksCardState().up).toBe(false);
  });
});

describe("the + and a draft's ring (window A's popover), switch on", () => {
  // The card's dialog, as the rail reads it (`popBusy`): standing, not mid-Create.
  let hadQuery: unknown = MISSING;
  beforeEach(() => {
    hadQuery = "querySelector" in doc ? doc.querySelector : MISSING;
    doc.querySelector = (sel: string) => (sel === ".modal-popover .modal-dialog" ? { hasAttribute: () => false } : null);
  });
  afterEach(() => {
    if (hadQuery === MISSING) delete doc.querySelector;
    else doc.querySelector = hadQuery;
  });
  /** The schedule and the drafts answer; everything else waits. */
  const answer = () => {
    (globalThis as { fetch: unknown }).fetch = (url: string) => {
      const u = String(url);
      if (/\/api\/schedule(\?|$)/.test(u)) return Promise.resolve(new Response(JSON.stringify({ permission_modes: [], entries: [] })));
      if (/\/api\/drafts(\?|$)/.test(u)) return Promise.resolve(new Response(JSON.stringify({ chat: {}, task: {} })));
      return new Promise<Response>(() => {});
    };
  };
  type Pop = { pinned?: boolean; held?: boolean; pop?: boolean; door?: string; chatTask?: Task };
  const card = () => getPlusCard<Pop>();
  /** The card as the shell's host would draw it: NewJobModal's props. */
  const drawn = () => (plusHostElement() as ReactElement<Record<string, never>> | null)?.props as Record<string, unknown> | undefined;
  const signal = () => (drawn()?.popover as { closeSignal: number } | undefined)?.closeSignal ?? 0;
  const pressPlus = async (r: ReactTestRenderer, detail: number) => {
    const plus = r.root.find((n) => n.type === "button" && n.props["data-rail-door"] === "plus");
    await act(async () => {
      plus.props.onClick(click(detail));
      await tick(20);
    });
  };

  test("the +: a click is temp (the pin icon hollow, grips on), one press outside closes it the chassis' way; its own door is not outside", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({ unified: true });
    await pressPlus(r, 1);
    expect(card()).toMatchObject({ pop: true, pinned: true, door: PLUS_DOOR });
    expect(card()!.held).toBeFalsy();
    const pin = drawn()!.pinNode as ReactElement<{ pinned: boolean }>;
    expect(pin.props.pinned).toBe(false);
    expect((drawn()!.popover as { resize?: unknown }).resize).toBeDefined();
    const before = signal();
    await pressAt(at(doorSelector("plus")));
    expect(signal()).toBe(before);
    await pressAt(at(".modal-popover"));
    expect(signal()).toBe(before);
    await pressAt(NOWHERE);
    expect(signal()).toBe(before + 1);
    off();
  });

  test("the +: the pin icon holds (outside presses and the frame ignored), and a double-click opens it pinned", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({ unified: true });
    await pressPlus(r, 1);
    await act(async () => (drawn()!.pinNode as ReactElement<{ onToggle: () => void }>).props.onToggle());
    expect(card()!.held).toBe(true);
    const before = signal();
    await pressAt(NOWHERE);
    await frameBlur();
    expect(signal()).toBe(before);
    // Unpinned (temp again), then a double-click on its own door: the first
    // click closes it (the chassis' fade), the second lands on the fade — and
    // once the fade is done the card opens again, pinned (row 8's blink).
    await act(async () => (drawn()!.pinNode as ReactElement<{ onToggle: () => void }>).props.onToggle());
    expect(card()!.held).toBe(false);
    const was = signal();
    await pressPlus(r, 1);
    expect(signal()).toBe(was + 1);
    await pressPlus(r, 2);
    await act(async () => {
      (drawn()!.onClose as () => void)();
      await tick(20);
    });
    expect(card()).toMatchObject({ pop: true, pinned: true, held: true });
    off();
  });

  test("the +: a lone second click (the first one closed it) opens it pinned", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({ unified: true });
    await pressPlus(r, 2);
    expect(card()).toMatchObject({ pop: true, pinned: true, held: true });
    off();
  });

  test("the switch off: no pin icon, and a preview has no grips", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({ unified: false });
    await pressPlus(r, 1);
    expect(drawn()!.pinNode).toBeUndefined();
    off();
  });

  test("a draft's ring: a click opens its popover temp, a double-click pinned; its ring is its own door", async () => {
    answer();
    const off = registerPlusHost();
    const draft = chat("d", { kind: "draft", draft_kind: "chat", session_id: "" } as Partial<Task>);
    const r = await mount({ unified: true, chats: [chat("a"), draft] });
    await press(listRing(r, "d"), 1);
    await act(async () => {
      await tick(20);
    });
    expect(card()).toMatchObject({ pop: true, pinned: true, door: "d" });
    expect(card()!.held).toBeFalsy();
    const before = signal();
    await pressAt(at(ringSelector(".claude-rail-chats", "d")));
    expect(signal()).toBe(before);
    await press(listRing(r, "d"), 2);
    await act(async () => {
      await tick(20);
    });
    expect(card()).toMatchObject({ door: "d", held: true });
    off();
  });
});

describe("a pinned task card across a hop and a list change (rows 13, 16–18)", () => {
  test("row 13: a temp card does not survive the rail's remount", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    await act(async () => r.update(<><Rail railKey="next" /><BotCardHost /></>));
    await settle();
    expect(shownKey(r)).toBeNull();
  });

  test("row 16: pinned, the next page lists the chat — it stays on that ring, pinned", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 2);
    await act(async () => r.update(<><Rail railKey="next" /><BotCardHost /></>));
    await settle();
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
  });

  /** The +'s seed answers (the schedule, the drafts); everything else waits. */
  const answerSeed = () => {
    (globalThis as { fetch: unknown }).fetch = (url: string) => {
      const u = String(url);
      if (/\/api\/schedule(\?|$)/.test(u)) return Promise.resolve(new Response(JSON.stringify({ permission_modes: [], entries: [] })));
      if (/\/api\/drafts(\?|$)/.test(u)) return Promise.resolve(new Response(JSON.stringify({ chat: {}, task: {} })));
      return new Promise<Response>(() => {});
    };
  };
  type Pop = { pop: boolean; pinned: boolean; held: boolean; door: string };

  test("pinned, the next page does not list the chat — the window closes, the pin with it (no + in its place)", async () => {
    const off = registerPlusHost();
    answerSeed();
    const r = await mount({ unified: true, chats: [chat("a"), chat("o", { project: OTHER, target: OTHER })] });
    await press(listRing(r, "a"), 2);
    expect(aPinned()).toBe("task:" + chat("a").key);
    await act(async () => r.update(<><Rail railKey="next" place={{ kind: "folder", dir: OTHER }} /><BotCardHost /></>));
    await act(async () => {
      await tick(30);
    });
    expect(shownKey(r)).toBeNull();
    expect(aPinned()).toBeNull();
    expect(getPlusCard()).toBeNull();
    off();
  });

  test("a pinned + New task card comes back pinned on the next rail (the mark `plus`)", async () => {
    const off = registerPlusHost();
    answerSeed();
    setAPinned("plus");
    const r = await mount({ unified: true });
    await act(async () => {
      await tick(30);
    });
    expect(shownKey(r)).toBeNull();
    expect(getPlusCard<Pop>()).toMatchObject({ pop: true, pinned: true, held: true, door: PLUS_DOOR });
    off();
  });

  test("a reload (a fresh rail, the mark kept): the pinned task's card comes back on its ring, pinned", async () => {
    setAPinned("task:" + chat("a").key);
    const r = await mount({ unified: true });
    await settle();
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
  });

  test("a reload with the mark on a task this view does not list: nothing opens, the mark goes", async () => {
    const off = registerPlusHost();
    answerSeed();
    setAPinned("task:gone");
    const r = await mount({ unified: true });
    await act(async () => {
      await tick(30);
    });
    expect(shownKey(r)).toBeNull();
    expect(aPinned()).toBeNull();
    expect(getPlusCard()).toBeNull();
    off();
  });

  test("a reload with the mark on the Tasks card: it comes back pinned once its card is mounted", async () => {
    const off = registerTasksCard();
    setAPinned("tasks");
    await mount({ unified: true });
    await settle();
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    off();
  });

  test("the switch off: the mark restores nothing", async () => {
    setAPinned("task:" + chat("a").key);
    const r = await mount({ unified: false });
    await settle();
    expect(shownKey(r)).toBeNull();
  });

  test("row 17: the pinned task's ring drops out of the list with no change of view — the window closes, the pin with it", async () => {
    const off = registerPlusHost();
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 2);
    await act(async () => {
      const moved = [chat("a", { project: OTHER, target: OTHER }), chat("b")];
      rememberListing(moved);
      publishTasks(moved as unknown as TaskPulseTask[]);
      await tick();
    });
    expect(shownKey(r)).toBeNull();
    expect(getPlusCard()).toBeNull();
    expect(aPinned()).toBeNull();
    off();
  });

  test("row 18: the pinned task archived — the window closes, the pin with it", async () => {
    const off = registerPlusHost();
    const r = await mount({ unified: true });
    await press(listRing(r, "a"), 2);
    await act(async () => {
      const gone = [chat("a", { status: "archived" }), chat("b")];
      rememberListing(gone);
      publishTasks(gone as unknown as TaskPulseTask[]);
      await tick();
    });
    expect(shownKey(r)).toBeNull();
    expect(getPlusCard()).toBeNull();
    expect(aPinned()).toBeNull();
    off();
  });

  test("the switch off: a pinned (held) card is gone with the hop, as it always was", async () => {
    const r = await mount({ unified: false });
    await press(listRing(r, "a"));
    expect(peek(r)!.props.held).toBe(true);
    await act(async () => r.update(<><Rail railKey="next" /><BotCardHost /></>));
    await settle();
    expect(shownKey(r)).toBeNull();
  });
});

describe("the pin's seat in the shared head", () => {
  const order = (r: ReactTestRenderer) =>
    r.root
      .findAll((n) => typeof n.type === "string" && (n.props.className === "task-head-acts"))[0]!
      .children.filter((c): c is ReactTestInstance => typeof c !== "string")
      .map((c) => String(c.props["aria-label"] ?? c.props.className));

  test("[×] [pin] — after a close lead; [preLead ×] [pin] [←] — before a back lead; no pin on a head that is not given one", async () => {
    const pin = <button type="button" aria-label="Pin" />;
    let r!: ReactTestRenderer;
    await act(async () => {
      r = create(<TaskHead lead={{ kind: "close", label: "Close", onPress() {} }} pinNode={pin} task={null} title="T" fitEnabled={false} />);
    });
    expect(order(r)).toEqual(["Close", "Pin"]);
    await act(async () => {
      r.update(
        <TaskHead
          preLead={<button type="button" aria-label="Close Tasks" />}
          lead={{ kind: "back", label: "Back", onPress() {} }}
          pinNode={pin}
          task={null}
          title="T"
          fitEnabled={false}
        />,
      );
    });
    expect(order(r)).toEqual(["Close Tasks", "Pin", "Back"]);
    await act(async () => {
      r.update(<TaskHead lead={{ kind: "close", label: "Close", onPress() {} }} task={null} title="T" fitEnabled={false} />);
    });
    expect(order(r)).toEqual(["Close"]);
    expect(r.root.findAll((n) => String(n.props?.className ?? "").includes("rail-pin-btn"))).toHaveLength(0);
    act(() => r.unmount());
  });

  test("the list card's head, switch on: × then the pin, hollow, its hint the double-click", async () => {
    const r = await mount({ unified: true });
    await press(listRing(r, "a"));
    const pins = peek(r)!.findAll((n) => n.type === "button" && String(n.props.className).includes("rail-pin-btn"));
    expect(pins).toHaveLength(1);
    expect(pins[0]!.props["aria-pressed"]).toBe(false);
    expect(pins[0]!.props["data-hint"]).toBe("Pin (double-click the ring)");
    await act(async () => pins[0]!.props.onClick());
    const now = peek(r)!.findAll((n) => n.type === "button" && String(n.props.className).includes("rail-pin-btn"))[0]!;
    expect(now.props["aria-pressed"]).toBe(true);
    expect(now.props.className).toContain("is-pinned");
    expect(now.props["data-hint"]).toBe("Unpin (double-click the ring)");
  });
});

// The bot ring selector is the classifier's too (used by the rail's own
// watchers' tests above through the stand-in element).
void botRingSelector;
