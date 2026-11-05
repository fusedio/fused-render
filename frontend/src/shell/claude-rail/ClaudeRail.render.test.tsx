// THE RAIL BESIDE THE OPEN PANE (ClaudeRail `paneOpen`; owner, 2026-11-05),
// mounted: the panel glyph and the pull seam only while the pane is shut;
// beside the open pane (favourites on) the rail stands only with something to
// draw — hidden, and off the favourite card's rail stack, when empty; and a
// chat the pane already holds is said in the card's body, not mounted twice.
// Real modules throughout — the flags and the rows are published into their
// own stores (`mock.module` is process-wide in bun and leaks across files).
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { Task, TaskPulseTask } from "@platform/lib/api";

const { ClaudeRail } = await import("./ClaudeRail");
const { RailPeek } = await import("./RailPeek");
const { publishTaskFavourites, publishFavouritesParent, resetTaskFavouritesFlagForTests } = await import(
  "@platform/lib/favourites-flag"
);
const { publishCanvasRailPeek, resetCanvasRailPeekFlagForTests } = await import("./rail-peek-flag");
const { publishTasks, rememberListing, forgetListing, resetListingFeedForTests } = await import("@shell/tasksPulse");
const { favPeekState, resetFavPeekStoreForTests } = await import("./fav-peek-store");

const DIR = "/home/me/apps/demo";

/** A chat in the rail's folder, every field the order reads at a default. */
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

/** A box, as `getBoundingClientRect` answers. */
function rect(left: number, top: number, width: number, height: number) {
  return { left, top, right: left + width, bottom: top + height, width, height, x: left, y: top };
}
/** The row the rail stands in: a wide window, room for the card. */
const ROW = { getBoundingClientRect: () => rect(0, 0, 1400, 900), parentElement: null };

/** What a ref sees in the browser, as much of it as the rail touches: every
 *  node a 48px column at the row's right edge (the rail, its rings). */
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

const realFetch = globalThis.fetch;
const g = globalThis as Record<string, unknown>;
// The measuring globals, stated outright for every test: bun runs every file
// in one process, and an earlier suite may leave its own behind (row-fit's
// `getComputedStyle` answers `node.__style` — undefined for these nodes), so
// a `??=` here would keep the stranger's. Whatever stood before goes back after.
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
  getComputedStyle: () => ({ display: "block", paddingTop: "0", paddingBottom: "0", rowGap: "0", getPropertyValue: () => "" }),
};
beforeEach(() => {
  for (const [name, stub] of Object.entries(measuring)) {
    saved.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
  resetTaskFavouritesFlagForTests();
  resetCanvasRailPeekFlagForTests();
  resetFavPeekStoreForTests();
  resetListingFeedForTests();
  forgetListing();
  // Nothing from the server: every read waits forever, so only what a test
  // publishes is ever on screen.
  (globalThis as { fetch: unknown }).fetch = () => new Promise<Response>(() => {});
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
  (globalThis as { fetch: unknown }).fetch = realFetch;
  for (const [name, was] of saved) {
    if (was === MISSING) delete g[name];
    else g[name] = was;
  }
  saved.clear();
});

async function mountRail(o: { paneOpen: boolean; favourites: boolean; chats?: Task[]; onOpenChat?: (href: string) => void }) {
  // The card's flag and the favourites' pair, as Settings would publish them.
  publishCanvasRailPeek({ enabled: true });
  publishFavouritesParent({ enabled: true });
  publishTaskFavourites({ enabled: o.favourites });
  const rows = o.chats ?? [];
  rememberListing(rows);
  publishTasks(rows as unknown as TaskPulseTask[]);
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      <ClaudeRail
        paneOpen={o.paneOpen}
        place={{ kind: "folder", dir: DIR }}
        newTask={{ target: DIR, waiting: null, on: null }}
        activeSessionId={null}
        onOpen={() => {}}
        onOpenChat={o.onOpenChat ?? (() => {})}
        onPullStart={() => {}}
      />,
      { createNodeMock: () => fakeEl() },
    );
  });
  mounted.push(r);
  await act(async () => {
    await new Promise((res) => setTimeout(res, 0));
  });
  return r;
}

const aside = (r: ReactTestRenderer) => r.root.findByType("aside");
const byLabel = (r: ReactTestRenderer, label: string): ReactTestInstance[] =>
  r.root.findAll((n) => typeof n.type === "string" && n.props["aria-label"] === label);
const glyph = (r: ReactTestRenderer) => byLabel(r, "Show the Claude panel").length;
const seam = (r: ReactTestRenderer) => byLabel(r, "Drag to show the Claude panel").length;

describe("ClaudeRail beside the pane", () => {
  for (const favourites of [false, true]) {
    test(`pane shut, favourites ${favourites ? "on" : "off"}: glyph and seam, standing even empty, registered`, async () => {
      const r = await mountRail({ paneOpen: false, favourites });
      expect(glyph(r)).toBe(1);
      expect(seam(r)).toBe(1);
      expect(aside(r).props.hidden).toBeUndefined();
      expect(favPeekState().rail).not.toBeNull();
    });
  }

  test("pane open, favourites off: no glyph, no seam, hidden even with a chat, not registered", async () => {
    const r = await mountRail({ paneOpen: true, favourites: false, chats: [chat("a")] });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(aside(r).props.hidden).toBe(true);
    expect(favPeekState().rail).toBeNull();
  });

  test("pane open, favourites on, nothing to draw: hidden and off the rail stack", async () => {
    const r = await mountRail({ paneOpen: true, favourites: true });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(aside(r).props.hidden).toBe(true);
    expect(favPeekState().rail).toBeNull();
  });

  test("pane open, favourites on, a chat here: standing and registered, still no glyph or seam", async () => {
    const r = await mountRail({ paneOpen: true, favourites: true, chats: [chat("a")] });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });
});

describe("ClaudeRail's card beside the pane", () => {
  /** Press the ring for `key`: the card opens on that chat. */
  async function press(r: ReactTestRenderer, key: string) {
    const link = r.root.find((n) => n.type === "a" && typeof n.props["aria-label"] === "string" && n.props["aria-label"].startsWith(key));
    await act(async () => {
      link.props.onClick({ preventDefault() {}, stopPropagation() {}, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, currentTarget: fakeEl() });
      await new Promise((res) => setTimeout(res, 0));
    });
  }

  const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
  let hadGetById = false;
  beforeEach(() => {
    installPortalContainer();
    // The card portals into the body: its refs are the body's to mock.
    (doc.body as { createNodeMock: unknown }).createNodeMock = () => fakeEl();
    hadGetById = "getElementById" in doc;
    doc.getElementById ??= () => null;
  });
  afterEach(() => {
    removePortalContainer();
    if (!hadGetById) delete doc.getElementById;
  });

  test("beside the open pane a ring press opens the chat in the pane: no card is pinned", async () => {
    const opened: string[] = [];
    const r = await mountRail({ paneOpen: true, favourites: true, chats: [chat("a")], onOpenChat: (h) => opened.push(h) });
    await press(r, "a");
    expect(opened).toHaveLength(1);
    expect(r.root.findAllByType(RailPeek)).toHaveLength(0);
  });

});
