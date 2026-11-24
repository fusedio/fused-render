// RAIL CARDS: HOVER / TEMP / PIN, MOUNTED (rail-pin-lib.ts; owner, 2026-11-16,
// 2026-11-17). Every door × the transitions that need the rail: a click is
// temp, one press outside (the page, not the rail) closes it, the pin icon —
// the only pin — pins and outside presses are ignored, the pin icon again is
// temp; a ring or door closes its own TEMP card (never a pinned one) and a
// double-click is open then close; the rail and window A / B are the view, so a press there closes
// no temp card; the hop rows 13–17. Real modules throughout, as ClaudeRail.render.test.tsx;
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
const { publishTasks, rememberListing, forgetListing, resetListingFeedForTests } = await import("@shell/tasksPulse");
const { favPeekState, resetFavPeekStoreForTests, resetFavHoverForTests } = await import("./fav-peek-store");
const { publishBots, resetBotsPulseForTests } = await import("@platform/lib/botsPulse");
const { setBotsAvailableForTests } = await import("@platform/lib/bots-available");
const { aPinned, botOpen, favOpen, favPinned, setAPinned, setBotOpen, setFavOpen, setFavPinned } = await import("@platform/lib/last-chat");
const { dropSharedPrefsRead } = await import("@platform/lib/api");
const { botCardState, resetBotCardForTests } = await import("./bot-card-store");
const { botsCardState, botsCardStore, registerBotsCard, resetBotsCardForTests } = await import("./bots-peek-store");
const { registerTasksCard, resetTasksCardForTests, tasksCardState, tasksCardStore } = await import("./tasks-peek-store");
const { forgetGitCardRail, gitCardIntent, gitCardState, gitCardStore, resetGitCardForTests, setGitCardRail, GIT_CARD_KEY } =
  await import("./git-peek-store");
const { resetFlagsForTests: resetClaudeFlagsForTests } = await import("@apps/claude/feature-flag");
const { getPlusCard, plusHostElement, registerPlusHost, resetPlusCardStoreForTests } = await import("./plus-card-store");
const { doorSelector, ringSelector, botRingSelector, viewPress } = await import("./rail-pin-lib");
const { PLUS_DOOR } = await import("./plus-pop-lib");
const { setWindowPosMark, windowPosMark } = await import("@platform/lib/last-chat");
const { commitWindowPos, resetRailMoveForTests, windowBox } = await import("./rail-move-store");
const { RAIL_HEAD_SELECTOR } = await import("./rail-move-lib");
const { windowABox, resetCardSnap } = await import("./card-snap");
const { _setUiLayoutForTest } = await import("@platform/lib/ui-layout");
const { snapGuide } = await import("./card-snap");

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
  viewPress.inside = false;
  (globalThis as { fetch: unknown }).fetch = realFetch;
  dropSharedPrefsRead();
  resetClaudeFlagsForTests();
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

function Rail({ place, ...rest }: { place?: RailPlace; railKey?: string }) {
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
  chats?: Task[];
  extra?: ReactElement | null;
  bots?: boolean;
}): Promise<ReactTestRenderer> {
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

/** An element the classifier reads, matching the selectors given (an
 *  `Element`, as the list's and the bot's watchers only read one). */
const at = (...matches: string[]) =>
  Object.assign(new (g.Element as new () => object)(), { closest: (s: string) => (matches.includes(s) ? {} : null) });
/** A press on the page: no rail, no window. */
const NOWHERE = at();
/** A press on the rail (its blank, or something on it given): the view. */
const onRail = (...matches: string[]) => at(".claude-rail", ...matches);

/** The pin icon in window A's task card head (RailPeek `pinCtl`). */
async function pinIcon(r: ReactTestRenderer) {
  await act(async () => peek(r)!.props.pinCtl.onToggle());
}

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

describe("the task ring's card (window A)", () => {
  test("a click is TEMP: one press outside closes it, and so does Escape; a PINNED card ignores Escape", async () => {
    const r = await mount({});
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
    await press(listRing(r, "a"));
    await pinIcon(r);
    expect(peek(r)!.props.pinCtl.pinned).toBe(true);
    await escapeOnBody();
    expect(shownKey(r)).toBe("a");
  });

  test("the rail is the view, not outside: its blank, another ring, a door or window B's card close no temp card", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    await pressAt(onRail());
    await pressAt(onRail(ringSelector(".claude-rail-chats", "b")));
    await pressAt(onRail(doorSelector("tasks")));
    await pressAt(at(".claude-rail-peek"));
    await pressAt(at(".modal-popover"));
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(false);
    // A menu window B portaled out (`viewPress`, raised in its capture phase).
    await act(async () => {
      for (const fn of [...(docL.get("pointerdown") ?? [])]) fn({ target: NOWHERE, button: 0 });
      viewPress.inside = true;
      await tick();
    });
    expect(shownKey(r)).toBe("a");
    // The page is outside.
    await pressAt(NOWHERE);
    expect(shownKey(r)).toBeNull();
  });

  test("the pin icon PINS (outside presses and the frame ignored), again is temp, then an outside press closes", async () => {
    const r = await mount({});
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
    const r = await mount({});
    await press(listRing(r, "a"));
    await frameBlur();
    expect(shownKey(r)).toBeNull();
  });

  test("its own ring toggles a TEMP card closed (owner, 2026-11-19); a pinned one stays up, pinned", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBe("a");
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBeNull();
    expect(aPinned()).toBeNull();
    await press(listRing(r, "a"));
    await pinIcon(r);
    await press(listRing(r, "a"));
    await press(listRing(r, "a"), 2);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
    expect(aPinned()).toBe("task:" + chat("a").key);
    // Unpinned: temp again, the mark gone; its ring now closes it.
    await pinIcon(r);
    expect(peek(r)!.props.held).toBe(false);
    expect(aPinned()).toBeNull();
    await press(listRing(r, "a"));
    expect(shownKey(r)).toBeNull();
  });

  test("a double-click is two clicks: it opens temp, then closes it; a lone detail-2 click opens temp", async () => {
    const r = await mount({});
    await press(listRing(r, "a"), 1);
    await press(listRing(r, "a"), 2);
    expect(shownKey(r)).toBeNull();
    expect(aPinned()).toBeNull();
    await press(listRing(r, "b"), 2);
    expect(shownKey(r)).toBe("b");
    expect(peek(r)!.props.held).toBe(false);
  });

  test("THE PIN IS THE WINDOW'S: another ring of a pinned window swaps what it shows, still pinned", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    await pinIcon(r);
    expect(peek(r)!.props.held).toBe(true);
    await press(listRing(r, "b"));
    expect(shownKey(r)).toBe("b");
    expect(peek(r)!.props.held).toBe(true);
    expect(aPinned()).toBe("task:" + chat("b").key);
  });

  test("no × with the pin: the head's first button is the pin", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    const labels = peek(r)!
      .findAll((n) => n.type === "button")
      .map((n) => String(n.props["aria-label"] ?? ""));
    expect(labels).not.toContain("Close the preview");
    expect(labels).toContain("Pin");
  });

  test("row 20: Enter on a ring (detail 0) is a click: temp", async () => {
    const r = await mount({});
    await press(listRing(r, "a"), 0);
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(false);
  });
});

describe("the favourite's card (window B)", () => {
  const starred = () => [chat("a"), chat("f", { starred: true })];

  test("a click is temp; one press outside closes it; its own ring toggles it closed; the pin icon pins and outside presses are ignored", async () => {
    const r = await mount({ chats: starred() });
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
    await pressAt(NOWHERE);
    expect(favOpen()).toBeNull();
    // A double-click is two clicks: open, then close — temp.
    await press(footRing(r, "f"), 1);
    await press(footRing(r, "f"), 2);
    expect(favOpen()).toBeNull();
    expect(favPinned()).toBeNull();
    await press(footRing(r, "f"));
    // The pin icon (FavouritePeek `togglePin`: the window's pin mark).
    await act(async () => setFavPinned("f"));
    await pressAt(NOWHERE);
    await frameBlur();
    await escapeOnBody();
    expect(favOpen()).toBe("f");
    // Its own ring: a pinned window stays pinned, a click or a detail-2 one.
    await press(footRing(r, "f"), 1);
    await press(footRing(r, "f"), 2);
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBe("f");
    // The pin icon again: temp; the ring closes it.
    await act(async () => setFavPinned(null));
    await press(footRing(r, "f"), 1);
    expect(favOpen()).toBeNull();
    await press(footRing(r, "f"));
    await pressAt(NOWHERE);
    expect(favOpen()).toBeNull();
    // Escape's one rule: with the focus nowhere it closes a temp card.
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    await escapeOnBody();
    expect(favOpen()).toBeNull();
  });

  test("its own ring and the rail are not an outside press; the card's own tree (a portal) is inside", async () => {
    const { favPress } = await import("./fav-peek-store");
    const r = await mount({ chats: starred() });
    await press(footRing(r, "f"));
    await pressAt(onRail(ringSelector(".claude-rail-favs", "f")));
    await pressAt(onRail());
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
    const r = await mount({ chats: starred() });
    await press(footRing(r, "f"));
    await frameBlur();
    expect(favOpen()).toBeNull();
  });
});

describe("window A and window B (rows 3, 4, 6)", () => {
  const starred = () => [chat("a"), chat("f", { starred: true })];

  test("A temp, a press on B's ring or B's card: A stays (the rail is the view), and B opens temp; a page press closes both", async () => {
    const r = await mount({ chats: starred() });
    await press(listRing(r, "a"));
    await pressAt(onRail(ringSelector(".claude-rail-favs", "f")));
    expect(shownKey(r)).toBe("a");
    await press(footRing(r, "f"));
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
    await pressAt(at(".claude-rail-peek"));
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(false);
    expect(favOpen()).toBe("f");
    await pressAt(NOWHERE);
    expect(shownKey(r)).toBeNull();
    expect(favOpen()).toBeNull();
  });

  test("B temp, a press on A's ring or A's card: B stays; a page press closes it", async () => {
    const r = await mount({ chats: starred() });
    await press(footRing(r, "f"));
    await pressAt(onRail(ringSelector(".claude-rail-chats", "a")));
    await pressAt(at(".claude-rail-peek"));
    expect(favOpen()).toBe("f");
    expect(favPinned()).toBeNull();
    await pressAt(NOWHERE);
    expect(favOpen()).toBeNull();
  });

  test("row 4: A pinned, B opened: both stay up; B pinned, A opened: both stay", async () => {
    const r = await mount({ chats: starred() });
    await press(listRing(r, "a"));
    await pinIcon(r);
    await pressAt(onRail(ringSelector(".claude-rail-favs", "f")));
    await press(footRing(r, "f"));
    await act(async () => setFavPinned("f"));
    expect(shownKey(r)).toBe("a");
    expect(favOpen()).toBe("f");
    await pressAt(onRail(ringSelector(".claude-rail-chats", "b")));
    await pressAt(NOWHERE);
    expect(shownKey(r)).toBe("a");
    expect(favOpen()).toBe("f");
  });
});

describe("the bot ring's card (window B)", () => {
  const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
  const ring = (r: ReactTestRenderer) => r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");

  const botPinIcon = (r: ReactTestRenderer) => act(async () => r.root.findByType(BotPeek).props.pinCtl.onToggle());

  test("temp: no mark for a reload, one press outside or Escape closes, the rail does not; pinned by the pin icon: the mark, outside ignored", async () => {
    const r = await mount({});
    await act(async () => {
      publishBots([BOT] as never);
    });
    await press(ring(r));
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: false });
    expect(botOpen()).toBeNull();
    expect(r.root.findByType(BotPeek).props.pinCtl.pinned).toBe(false);
    // The rail and window A's card are the view.
    await pressAt(onRail());
    await pressAt(onRail(ringSelector(".claude-rail-chats", "a")));
    await pressAt(at(".claude-rail-peek"));
    expect(botCardState()).toMatchObject({ key: "b1", held: false });
    await pressAt(NOWHERE);
    expect(botCardState().key).toBeNull();
    // Escape's one rule: temp closes on it, pinned (below) does not.
    await press(ring(r));
    await escapeOnBody();
    expect(botCardState().key).toBeNull();
    // A double-click is two clicks: open, then close.
    await press(ring(r), 1);
    await press(ring(r), 2);
    expect(botCardState().key).toBeNull();
    await press(ring(r));
    expect(botCardState()).toMatchObject({ key: "b1", held: false });
    await botPinIcon(r);
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    await escapeOnBody();
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    expect(botOpen()).toBe("b1");
    await pressAt(NOWHERE);
    expect(botCardState().key).toBe("b1");
    // Its ring: a pinned window stays pinned.
    await press(ring(r), 1);
    await press(ring(r), 2);
    expect(botCardState()).toMatchObject({ key: "b1", held: true });
    // The pin icon again: temp, the mark gone.
    await botPinIcon(r);
    expect(botCardState().held).toBe(false);
    expect(botOpen()).toBeNull();
    // Its ring closes a temp card; an outside press does too.
    expect(botCardState()).toMatchObject({ key: "b1", held: false });
    await press(ring(r), 1);
    expect(botCardState().key).toBeNull();
    await press(ring(r), 1);
    expect(botCardState()).toMatchObject({ key: "b1", held: false });
    await pressAt(NOWHERE);
    expect(botCardState().key).toBeNull();
  });

  test("row 15 / 13: a page hop keeps a pinned bot card and closes a temp one", async () => {
    const Host = ({ n }: { n: number }) => <BotCardHost key="host" data-n={n} />;
    const r = await mount({ bots: false, extra: <Host n={1} /> });
    await act(async () => {
      publishBots([BOT] as never);
    });
    await press(ring(r));
    await botPinIcon(r);
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
    const r = await mount({});
    await act(async () => {
      publishBots([BOT] as never);
      await tick();
    });
    void r;
    expect(botCardState()).toMatchObject({ key: "b1", pinned: true, held: true });
  });
});

describe("the Tasks, Bots and Git doors' cards (window A)", () => {
  test("Tasks: a press is temp, one press outside closes it; its own door and the rail are not outside; its door closes a temp card, never pins it; the pin icon pins; the frame closes temp only", async () => {
    const off = registerTasksCard();
    const r = await mount({});
    const door = () => byLabel(r, "Tasks")[0]!;
    expect(door().props["data-rail-door"]).toBe("tasks");
    await press(door());
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, held: false });
    await pressAt(onRail(doorSelector("tasks")));
    await pressAt(onRail());
    await pressAt(onRail(ringSelector(".claude-rail-favs", "a")));
    expect(tasksCardState().up).toBe(true);
    await pressAt(NOWHERE);
    expect(tasksCardState().up).toBe(false);
    await press(door(), 1);
    await frameBlur();
    expect(tasksCardState().up).toBe(false);
    // A double-click is two clicks: open, then close.
    await press(door(), 1);
    await press(door(), 2);
    expect(tasksCardState().up).toBe(false);
    await press(door(), 1);
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, held: false });
    expect(aPinned()).toBeNull();
    // The pin icon (DoorCard `togglePin`).
    await act(async () => tasksCardStore.setHeld(true));
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    await pressAt(NOWHERE);
    await frameBlur();
    // …and Escape with the focus nowhere, which closed a clicked Tasks card
    // before the pin, leaves a pinned one up.
    await escapeOnBody();
    expect(tasksCardState().up).toBe(true);
    expect(aPinned()).toBe("tasks");
    // Its own door: a pinned window stays pinned, a click or a detail-2 one.
    await press(door(), 1);
    await press(door(), 2);
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    // The pin icon again: temp; its door closes it.
    await act(async () => tasksCardStore.setHeld(false));
    expect(tasksCardState()).toMatchObject({ up: true, pinned: true, held: false });
    expect(aPinned()).toBeNull();
    await press(door(), 1);
    expect(tasksCardState().up).toBe(false);
    await press(door(), 1);
    await pressAt(NOWHERE);
    expect(tasksCardState().up).toBe(false);
    off();
  });

  test("Bots: the same — temp closes outside only, its door closes a temp card, never pins it, the pin icon pins", async () => {
    const off = registerBotsCard();
    const r = await mount({});
    const door = () => byLabel(r, "Bots")[0]!;
    await press(door());
    expect(botsCardState()).toMatchObject({ up: true, pinned: true, held: false });
    await pressAt(onRail());
    expect(botsCardState().up).toBe(true);
    await pressAt(NOWHERE);
    expect(botsCardState().up).toBe(false);
    await press(door(), 1);
    await press(door(), 2);
    expect(botsCardState().up).toBe(false);
    await press(door(), 1);
    expect(botsCardState()).toMatchObject({ up: true, held: false });
    await act(async () => botsCardStore.setHeld(true));
    expect(botsCardState()).toMatchObject({ up: true, held: true });
    await press(door(), 2);
    await pressAt(NOWHERE);
    expect(botsCardState()).toMatchObject({ up: true, held: true });
    off();
  });

  test("Git: temp closes outside and on its rail going; pinned stays through both", async () => {
    const r = await mount({});
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

describe("the + and a draft's ring (window A's popover)", () => {
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
  /** The pin icon in the popover's head (NewJobModal's `pinNode`). */
  const popPinIcon = () => act(async () => (drawn()!.pinNode as ReactElement<{ onToggle: () => void }>).props.onToggle());
  const pressPlus = async (r: ReactTestRenderer, detail: number) => {
    const plus = r.root.find((n) => n.type === "button" && n.props["data-rail-door"] === "plus");
    await act(async () => {
      plus.props.onClick(click(detail));
      await tick(20);
    });
  };

  test("the +: a click is temp (the pin icon hollow, grips on), one press outside closes it the chassis' way; its own door and the rail are not outside", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({});
    await pressPlus(r, 1);
    expect(card()).toMatchObject({ pop: true, pinned: true, door: PLUS_DOOR });
    expect(card()!.held).toBeFalsy();
    const pin = drawn()!.pinNode as ReactElement<{ pinned: boolean }>;
    expect(pin.props.pinned).toBe(false);
    expect((drawn()!.popover as { resize?: unknown }).resize).toBeDefined();
    const before = signal();
    await pressAt(onRail(doorSelector("plus")));
    await pressAt(onRail());
    expect(signal()).toBe(before);
    await pressAt(at(".modal-popover"));
    expect(signal()).toBe(before);
    await pressAt(NOWHERE);
    expect(signal()).toBe(before + 1);
    off();
  });

  test("the +: the pin icon holds (outside presses, the frame and its own door ignored); unpinned, its door closes it", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({});
    await pressPlus(r, 1);
    await popPinIcon();
    expect(card()!.held).toBe(true);
    const before = signal();
    await pressAt(NOWHERE);
    await frameBlur();
    await pressPlus(r, 1);
    await pressPlus(r, 2);
    expect(signal()).toBe(before);
    expect(card()).toMatchObject({ pop: true, held: true });
    // Unpinned (temp again): its own door closes it (owner, 2026-11-19).
    await popPinIcon();
    expect(card()!.held).toBe(false);
    await pressPlus(r, 1);
    expect(signal()).toBe(before + 1);
    off();
  });

  test("the +: a lone detail-2 click opens it temp (no double-click pin)", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({});
    await pressPlus(r, 2);
    expect(card()).toMatchObject({ pop: true, pinned: true, door: PLUS_DOOR });
    expect(card()!.held).toBeFalsy();
    off();
  });

  test("the +, pinned and moved (owner, 2026-11-17): it floats at window A's position — no box published, the right grips on — and docks with the unpin", async () => {
    answer();
    const off = registerPlusHost();
    const r = await mount({});
    await pressPlus(r, 1);
    type Pop = {
      place: (w: { width: number; height: number }) => unknown;
      onBox?: unknown;
      move?: { className: string };
      resize?: { grips?: readonly string[] };
    };
    const pop = () => drawn()!.popover as Pop;
    // Temp: the head does not grab.
    expect(pop().move!.className).not.toContain("is-movable");
    await act(async () => (drawn()!.pinNode as ReactElement<{ onToggle: () => void }>).props.onToggle());
    expect(pop().move!.className).toContain("is-movable");
    expect(pop().onBox).toBeDefined();
    await act(async () => commitWindowPos("a", { left: 150, top: 90 }));
    await settle();
    expect(pop().move!.className).toContain("is-floating");
    expect(pop().place({ width: 420, height: 640 })).toEqual({ left: 150, top: 90, width: 420, height: 640 });
    // Floating, its box feeds no docked placement (card-snap) — only the
    // live box window B's head drag snaps to (rail-move-store, round 3).
    resetCardSnap();
    (pop().onBox as (b: unknown) => void)({ left: 150, top: 90, width: 420, height: 640 });
    expect(windowABox()).toBeNull();
    expect(windowBox("a")).toEqual({ left: 150, top: 90, width: 420, height: 640 });
    expect(pop().resize!.grips).toContain("br");
    // The unpin: docked again, the position gone with the pin.
    await act(async () => (drawn()!.pinNode as ReactElement<{ onToggle: () => void }>).props.onToggle());
    await settle();
    expect(windowPosMark("a")).toBeNull();
    expect(pop().move!.className).not.toContain("is-floating");
    expect(pop().resize!.grips).toBeUndefined();
    off();
  });

  test("a draft's ring: a click opens its popover temp; its ring is its own door: it closes a temp card, never pins it; the pin icon pins", async () => {
    answer();
    const off = registerPlusHost();
    const draft = chat("d", { kind: "draft", draft_kind: "chat", session_id: "" } as Partial<Task>);
    const r = await mount({ chats: [chat("a"), draft] });
    await press(listRing(r, "d"), 1);
    await act(async () => {
      await tick(20);
    });
    expect(card()).toMatchObject({ pop: true, pinned: true, door: "d" });
    expect(card()!.held).toBeFalsy();
    const before = signal();
    await pressAt(onRail(ringSelector(".claude-rail-chats", "d")));
    expect(signal()).toBe(before);
    await press(listRing(r, "d"), 1);
    await act(async () => {
      await tick(20);
    });
    expect(signal()).toBe(before + 1);
    await popPinIcon();
    expect(card()).toMatchObject({ door: "d", held: true });
    off();
  });
});

describe("a pinned task card across a hop and a list change (rows 13, 16–18)", () => {
  test("row 13: a temp card does not survive the rail's remount", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    await act(async () => r.update(<><Rail railKey="next" /><BotCardHost /></>));
    await settle();
    expect(shownKey(r)).toBeNull();
  });

  test("row 16: pinned, the next page lists the chat — it stays on that ring, pinned", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    await pinIcon(r);
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
    const r = await mount({ chats: [chat("a"), chat("o", { project: OTHER, target: OTHER })] });
    await press(listRing(r, "a"));
    await pinIcon(r);
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
    const r = await mount({});
    await act(async () => {
      await tick(30);
    });
    expect(shownKey(r)).toBeNull();
    expect(getPlusCard<Pop>()).toMatchObject({ pop: true, pinned: true, held: true, door: PLUS_DOOR });
    off();
  });

  test("a reload (a fresh rail, the mark kept): the pinned task's card comes back on its ring, pinned", async () => {
    setAPinned("task:" + chat("a").key);
    const r = await mount({});
    await settle();
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
  });

  test("a reload with the mark on a task this view does not list: nothing opens, the mark goes", async () => {
    const off = registerPlusHost();
    answerSeed();
    setAPinned("task:gone");
    const r = await mount({});
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
    await mount({});
    await settle();
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    off();
  });

  test("row 17: the pinned task's ring drops out of the list with no change of view — the window closes, the pin with it", async () => {
    const off = registerPlusHost();
    const r = await mount({});
    await press(listRing(r, "a"));
    await pinIcon(r);
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
    const r = await mount({});
    await press(listRing(r, "a"));
    await pinIcon(r);
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

  test("the list card's head: the pin, hollow, its hint \"Pin\"; pressed, \"Unpin\"", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    const pins = peek(r)!.findAll((n) => n.type === "button" && String(n.props.className).includes("rail-pin-btn"));
    expect(pins).toHaveLength(1);
    expect(pins[0]!.props["aria-pressed"]).toBe(false);
    expect(pins[0]!.props["data-hint"]).toBe("Pin");
    await act(async () => pins[0]!.props.onClick());
    const now = peek(r)!.findAll((n) => n.type === "button" && String(n.props.className).includes("rail-pin-btn"))[0]!;
    expect(now.props["aria-pressed"]).toBe(true);
    expect(now.props.className).toContain("is-pinned");
    expect(now.props["data-hint"]).toBe("Unpin");
  });
});

// The bot ring selector is the classifier's too (used by the rail's own
// watchers' tests above through the stand-in element).
void botRingSelector;

// ---------------------------------------------------------------------------
// A PINNED WINDOW MOVES (rail-move-lib; owner, 2026-11-17): a pinned window
// dragged by its head floats at the WINDOW's position — kept through a swap to
// another ring or door, dropped with the pin, back after a reload — and while
// it floats it publishes no box for the other window to keep clear of.

describe("a pinned window moves by its head (owner, 2026-11-17)", () => {
  beforeEach(() => {
    resetRailMoveForTests();
    resetCardSnap();
  });
  afterEach(() => {
    resetRailMoveForTests();
    resetCardSnap();
  });

  type Style = { left: number; top: number; width: number; height?: number };
  /** The card's fixed box (the element `place()` positions). */
  const boxOf = (r: ReactTestRenderer, id: string): ReactTestInstance =>
    r.root.find((n) => n.type === "div" && n.props.id === id && n.props.role === "dialog");
  const listBox = (r: ReactTestRenderer) => boxOf(r, peek(r)!.props.id as string);
  const styleOf = (n: ReactTestInstance) => n.props.style as Style;
  /** A press on the head's bare area (`RAIL_HEAD_SELECTOR`, no control). */
  const HEAD = { closest: (sel: string) => (sel === RAIL_HEAD_SELECTOR ? {} : null) };
  const boxEl = (n: ReactTestInstance) => {
    const st = styleOf(n);
    return {
      contains: () => true,
      getBoundingClientRect: () => rect(st.left, st.top, st.width, st.height ?? 600),
      addEventListener() {},
      removeEventListener() {},
      setPointerCapture() {},
      hasPointerCapture: () => false,
      releasePointerCapture() {},
    };
  };
  const fireWin = (type: string, e: Record<string, unknown>) => {
    for (const fn of [...(winL.get(type) ?? [])]) fn({ type, ...e });
  };
  /** A head drag by (dx, dy): down on the head, one move, up. */
  async function dragHead(n: ReactTestInstance, dx: number, dy: number, target: unknown = HEAD) {
    const el = boxEl(n);
    const at = styleOf(n);
    await act(async () => {
      n.props.onPointerDown({ button: 0, pointerId: 7, clientX: at.left + 50, clientY: at.top + 10, currentTarget: el, target });
    });
    await act(async () => {
      fireWin("pointermove", { pointerId: 7, clientX: at.left + 50 + dx, clientY: at.top + 10 + dy });
      await tick();
    });
    await act(async () => {
      fireWin("pointerup", { pointerId: 7 });
      await tick();
    });
  }
  async function pinList(r: ReactTestRenderer, key = "a") {
    await press(listRing(r, key));
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await settle();
  }

  test("a head drag floats the pinned list card at window A's position; no box is published while it floats", async () => {
    const r = await mount({});
    await pinList(r);
    const docked = { ...styleOf(listBox(r)) };
    expect(windowABox()).not.toBeNull();
    await dragHead(listBox(r), -300, 50);
    await settle();
    const want = { left: docked.left - 300, top: docked.top + 50 };
    expect(JSON.parse(windowPosMark("a") ?? "null")).toEqual(want);
    expect(styleOf(listBox(r))).toMatchObject(want);
    expect(listBox(r).props.className).toContain("is-floating");
    // Window B places itself as if this one were not there (card-snap.ts).
    expect(windowABox()).toBeNull();
  });

  test("a press that never passes the threshold moves nothing; a hover or temp card never moves", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    const temp = { ...styleOf(listBox(r)) };
    await dragHead(listBox(r), -300, 50);
    await settle();
    expect(windowPosMark("a")).toBeNull();
    expect(styleOf(listBox(r))).toMatchObject({ left: temp.left, top: temp.top });
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await settle();
    await dragHead(listBox(r), 2, 2);
    expect(windowPosMark("a")).toBeNull();
    // …nor does a press on a control in the head.
    await dragHead(listBox(r), -300, 50, { closest: () => ({}) });
    expect(windowPosMark("a")).toBeNull();
  });

  test("the position is the window's: a swap to another ring and to the Tasks door keeps it", async () => {
    const off = registerTasksCard();
    let api: { visible: boolean } | null = null;
    const card = (
      <DoorCard store={tasksCardStore} href="/tasks" className="t" ariaLabel="Tasks" subOpen={false}>
        {(a) => {
          api = a;
          return null;
        }}
      </DoorCard>
    );
    const r = await mount({ extra: card });
    await pinList(r);
    await act(async () => commitWindowPos("a", { left: 120, top: 60 }));
    await settle();
    expect(styleOf(listBox(r))).toMatchObject({ left: 120, top: 60 });
    // Another ring of the pinned window: it swaps, pinned, at the same place.
    await press(listRing(r, "b"));
    await settle();
    expect(shownKey(r)).toBe("b");
    expect(peek(r)!.props.held).toBe(true);
    expect(styleOf(listBox(r))).toMatchObject({ left: 120, top: 60 });
    // The Tasks door: the card swaps to the door's, still at the window's place.
    await press(r.root.find((n) => n.type === "a" && n.props["data-rail-door"] === "tasks"));
    await settle();
    expect(tasksCardState()).toMatchObject({ up: true, held: true });
    expect(aPinned()).toBe("tasks");
    expect(windowPosMark("a")).not.toBeNull();
    expect(styleOf(boxOf(r, tasksCardStore.id))).toMatchObject({ left: 120, top: 60 });
    expect(api!.visible).toBe(true);
    off();
  });

  test("unpinned, the card docks and the position goes with the pin; a double-click on the head docks and keeps the pin", async () => {
    const r = await mount({});
    await pinList(r);
    const docked = { ...styleOf(listBox(r)) };
    await act(async () => commitWindowPos("a", { left: 120, top: 60 }));
    await settle();
    // The double-click on the bare head: docked, still pinned.
    await act(async () => {
      listBox(r).props.onDoubleClick({ target: HEAD, currentTarget: boxEl(listBox(r)) });
    });
    await settle();
    expect(windowPosMark("a")).toBeNull();
    expect(peek(r)!.props.held).toBe(true);
    expect(styleOf(listBox(r))).toMatchObject({ left: docked.left, top: docked.top });
    // Floating again, then the unpin: docked at once, the mark cleared.
    await act(async () => commitWindowPos("a", { left: 120, top: 60 }));
    await settle();
    expect(styleOf(listBox(r))).toMatchObject({ left: 120, top: 60 });
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await settle();
    expect(aPinned()).toBeNull();
    expect(windowPosMark("a")).toBeNull();
    expect(styleOf(listBox(r))).toMatchObject({ left: docked.left, top: docked.top });
  });

  test("a reload brings the pinned window back where it floated (the mark)", async () => {
    setAPinned("task:a");
    setWindowPosMark("a", JSON.stringify({ left: 210, top: 140 }));
    const r = await mount({});
    await settle();
    expect(shownKey(r)).toBe("a");
    expect(peek(r)!.props.held).toBe(true);
    expect(styleOf(listBox(r))).toMatchObject({ left: 210, top: 140 });
  });

  test("window B: a floating bot card publishes no box for window A to keep clear of; pressed last, window A steps over it", async () => {
    const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
    const r = await mount({});
    await act(async () => {
      publishBots([BOT] as never);
    });
    const botRing = r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");
    // The pin icon is the only way to pin (2026-11-17: no double-click pin).
    await press(botRing);
    await act(async () => r.root.findByType(BotPeek).props.pinCtl.onToggle());
    await settle();
    expect(botOpen()).toBe("b1");
    expect(favPeekState().favBox).not.toBeNull();
    await act(async () => commitWindowPos("b", { left: 40, top: 30 }));
    await settle();
    // Window B lets its box go a beat after it stops standing there
    // (fav-peek-store `FAV_BOX_RELEASE_MS`).
    await act(async () => {
      await new Promise((done) => setTimeout(done, 120));
    });
    expect(favPeekState().favBox).toBeNull();
    expect(styleOf(boxOf(r, r.root.findByType(BotPeek).props.id as string))).toMatchObject({ left: 40, top: 30 });
    // Window A, opened after it: on top at once (rail-move-lib `raised`; a
    // card coming up takes the front, or its hover opened UNDER a window
    // floating beside the rail — owner, 2026-11-17).
    const botBox = () => boxOf(r, r.root.findByType(BotPeek).props.id as string);
    expect(botBox().props.className).toContain("is-front");
    await press(listRing(r, "a"));
    expect(listBox(r).props.className).toContain("is-front");
    expect(botBox().props.className).not.toContain("is-front");
    // …a press inside the other one hands the front back, and a press inside
    // this one takes it again.
    await act(async () => botBox().props.onPointerDownCapture());
    expect(listBox(r).props.className).not.toContain("is-front");
    expect(botBox().props.className).toContain("is-front");
    await act(async () => listBox(r).props.onPointerDownCapture());
    expect(listBox(r).props.className).toContain("is-front");
  });

});

describe("floating windows (round 3 B; owner, 2026-11-17)", () => {
  let hadInner: { w: unknown; h: unknown } = { w: undefined, h: undefined };
  beforeEach(() => {
    hadInner = { w: win.innerWidth, h: win.innerHeight };
    resetRailMoveForTests();
    resetCardSnap();
    _setUiLayoutForTest();
  });
  afterEach(() => {
    win.innerWidth = hadInner.w;
    win.innerHeight = hadInner.h;
    resetRailMoveForTests();
    resetCardSnap();
    _setUiLayoutForTest();
  });

  type Style = { left: number; top: number; width: number; height?: number };
  const boxOf = (r: ReactTestRenderer, id: string): Style =>
    r.root.find((n) => n.type === "div" && n.props.id === id && n.props.role === "dialog").props.style as Style;
  const listStyle = (r: ReactTestRenderer) => boxOf(r, peek(r)!.props.id as string);
  const botStyle = (r: ReactTestRenderer) => boxOf(r, r.root.findByType(BotPeek).props.id as string);
  const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
  const botRing = (r: ReactTestRenderer) => r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");
  /** Window B: the bot's card, opened and PINNED. */
  async function openBot(r: ReactTestRenderer) {
    await act(async () => {
      publishBots([BOT] as never);
    });
    await press(botRing(r), 1);
    await settle();
  }

  test("back where I left it: a window shrink squeezes the floating card, the stored spot stays, and it returns as the room does", async () => {
    win.innerWidth = 1400;
    win.innerHeight = 900;
    const r = await mount({});
    await press(listRing(r, "a"));
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await settle();
    await act(async () => commitWindowPos("a", { left: 900, top: 200 }));
    await settle();
    expect(listStyle(r)).toMatchObject({ left: 900, top: 200, width: 420, height: 640 });
    const resize = async (w: number, h: number) => {
      win.innerWidth = w;
      win.innerHeight = h;
      await act(async () => {
        for (const fn of [...(winL.get("resize") ?? [])]) fn({ type: "resize" });
        await tick();
      });
    };
    await resize(1000, 700);
    expect(listStyle(r)).toMatchObject({ left: 1000 - 420, top: 700 - 640, height: 640 });
    expect(JSON.parse(windowPosMark("a") ?? "null")).toEqual({ left: 900, top: 200 });
    await resize(1400, 900);
    expect(listStyle(r)).toMatchObject({ left: 900, top: 200 });
    expect(JSON.parse(windowPosMark("a") ?? "null")).toEqual({ left: 900, top: 200 });
  });

  test("the edge magnet: a head drag within 12px of window B's card lands flush with it, the guide along the edge until the release", async () => {
    const r = await mount({});
    await openBot(r);
    await press(listRing(r, "a"));
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    await settle();
    const b = { ...botStyle(r) } as Required<Style>;
    const a = { ...listStyle(r) } as Required<Style>;
    const el = {
      contains: () => true,
      getBoundingClientRect: () => rect(a.left, a.top, a.width, a.height),
      addEventListener() {},
      removeEventListener() {},
      setPointerCapture() {},
      hasPointerCapture: () => false,
      releasePointerCapture() {},
    };
    const node = r.root.find((n) => n.type === "div" && n.props.id === peek(r)!.props.id && n.props.role === "dialog");
    const HEAD = { closest: (sel: string) => (sel === RAIL_HEAD_SELECTOR ? {} : null) };
    const fire = (type: string, e: Record<string, unknown>) => {
      for (const fn of [...(winL.get(type) ?? [])]) fn({ type, ...e });
    };
    // Left, so the card's right edge stops 5px short of window B's left
    // edge; and up 37px, clear of every edge across.
    const dx = b.left - 5 - (a.left + a.width) - 200;
    await act(async () => {
      node.props.onPointerDown({ button: 0, pointerId: 3, clientX: 500, clientY: 300, currentTarget: el, target: HEAD });
    });
    await act(async () => {
      fire("pointermove", { pointerId: 3, clientX: 500 + dx, clientY: 337 });
      await tick();
    });
    // 200px further left than flush: nothing near, no guide.
    expect(snapGuide()).toBeNull();
    await act(async () => {
      fire("pointermove", { pointerId: 3, clientX: 500 + dx + 200, clientY: 337 });
      await tick();
    });
    expect(snapGuide()).toMatchObject({ x: b.left });
    await act(async () => {
      fire("pointerup", { pointerId: 3 });
      await tick();
    });
    expect(JSON.parse(windowPosMark("a") ?? "null")).toEqual({ left: b.left - a.width, top: a.top + 37 });
    expect(snapGuide()).toBeNull();
  });
});

describe("the pin on the door of a pinned window (owner, 2026-11-18)", () => {
  const marksIn = (n: ReactTestInstance) => n.findAll((c) => c.props?.className === "claude-rail-pinmark");
  const marks = (r: ReactTestRenderer) => marksIn(r.root);

  test("window A: no mark on a temp card's ring; the pin icon puts it on that ring, and takes it off again", async () => {
    const r = await mount({});
    await press(listRing(r, "a"));
    expect(marks(r)).toHaveLength(0);
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    expect(marksIn(listRing(r, "a"))).toHaveLength(1);
    expect(marks(r)).toHaveLength(1);
    await act(async () => peek(r)!.props.pinCtl.onToggle());
    expect(marks(r)).toHaveLength(0);
  });

  test("window B: the pinned favourite's ring in the favourites, never the list's ring on the same chat", async () => {
    const r = await mount({ chats: [chat("a"), chat("f", { starred: true })] });
    await press(footRing(r, "f"));
    expect(marks(r)).toHaveLength(0);
    await act(async () => setFavPinned("f"));
    expect(marksIn(footRing(r, "f"))).toHaveLength(1);
    expect(marks(r)).toHaveLength(1);
    await act(async () => setFavPinned(null));
    expect(marks(r)).toHaveLength(0);
  });

  test("the Tasks door: the mark while its card is pinned", async () => {
    const off = registerTasksCard();
    const r = await mount({});
    const door = () => byLabel(r, "Tasks")[0]!;
    await press(door());
    expect(marks(r)).toHaveLength(0);
    await act(async () => tasksCardStore.setHeld(true));
    expect(marksIn(door())).toHaveLength(1);
    off();
  });

});
