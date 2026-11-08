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
const { publishBots, resetBotsPulseForTests } = await import("@platform/lib/botsPulse");
const { setBotsAvailableForTests, setLauncherAvailableForTests } = await import("@platform/lib/bots-available");
const { announcePaneSession } = await import("@platform/lib/peek-session");
const { botOpen, setBotOpen, setFavOpen } = await import("@platform/lib/last-chat");
const { dropSharedPrefsRead } = await import("@platform/lib/api");
const { BotPeek, CHAT_LINGER_MS } = await import("./BotPeek");
const { BotCardHost } = await import("./BotCardHost");
const { resetBotCardForTests } = await import("./bot-card-store");
const { railGitNow, resetRailGitStoreForTests } = await import("./rail-git-store");

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
  // The bot card's thread (apps/bots Thread) watches its rows into view.
  IntersectionObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
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
  // publishes is ever on screen. Bots are offered (a Mac's server).
  (globalThis as { fetch: unknown }).fetch = () => new Promise<Response>(() => {});
  setBotsAvailableForTests(true);
  resetBotCardForTests();
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
  setBotsAvailableForTests(null);
  (globalThis as { fetch: unknown }).fetch = realFetch;
  for (const [name, was] of saved) {
    if (was === MISSING) delete g[name];
    else g[name] = was;
  }
  saved.clear();
});

async function mountRail(o: {
  paneOpen: boolean;
  favourites: boolean;
  chats?: Task[];
  onOpenChat?: (href: string) => void;
  /** The session the page's URL names — beside the open pane, the chat it
   *  shows; null is the pane's home (2026-11-05, `listHidden`). */
  activeSessionId?: string | null;
  /** The rail flag; on unless said. */
  peek?: boolean;
  /** The page's git (ClaudeRail `git`). */
  git?: { path: string; onOpenGit?: () => void };
  /** Mount the shell's bot card (BotCardHost) beside the rail, as App does. */
  botHost?: boolean;
}) {
  // The card's flag and the favourites' pair, as Settings would publish them.
  publishCanvasRailPeek({ enabled: o.peek ?? true });
  publishFavouritesParent({ enabled: true });
  publishTaskFavourites({ enabled: o.favourites });
  const rows = o.chats ?? [];
  rememberListing(rows);
  publishTasks(rows as unknown as TaskPulseTask[]);
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      <>
        <ClaudeRail
          paneOpen={o.paneOpen}
          place={{ kind: "folder", dir: DIR }}
          newTask={{ target: DIR, waiting: null, on: null }}
          activeSessionId={o.activeSessionId ?? null}
          onOpen={() => {}}
          onOpenChat={o.onOpenChat ?? (() => {})}
          onPullStart={() => {}}
          {...(o.git ? { git: o.git } : {})}
        />
        {o.botHost !== false && <BotCardHost />}
      </>,
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
// THE NAV DOORS (rail flag on, as every mount here; owner, 2026-11-07, order
// 2026-11-08): Search (where the app has the launcher panel), Bots, Tasks and
// the + where the panel glyph stood — always standing; the pinned bots at the
// foot.
const doors = (r: ReactTestRenderer) => byLabel(r, "Tasks").length + byLabel(r, "Bots").length;
/** The nav column's doors, top to bottom, by label. */
const navOrder = (r: ReactTestRenderer): string[] => {
  const nav = r.root.findAll((n) => n.props?.className === "claude-rail-nav");
  if (nav.length !== 1) return [];
  return nav[0]!
    .findAll((n) => typeof n.type === "string" && typeof n.props["aria-label"] === "string" && n.props.className?.includes("sidebar-rail-btn"))
    .map((n) => String(n.props["aria-label"]));
};

describe("ClaudeRail beside the pane", () => {
  for (const favourites of [false, true]) {
    test(`pane shut, favourites ${favourites ? "on" : "off"}: Tasks and Bots doors in place of the glyph, seam, standing even empty, registered`, async () => {
      const r = await mountRail({ paneOpen: false, favourites });
      expect(glyph(r)).toBe(0);
      expect(doors(r)).toBe(2);
      expect(seam(r)).toBe(1);
      expect(aside(r).props.hidden).toBeUndefined();
      expect(favPeekState().rail).not.toBeNull();
    });
  }

  for (const favourites of [false, true]) {
    test(`a server without Bots (Windows), favourites ${favourites ? "on" : "off"}: Tasks stands, no Bots door, no pinned bots (Bugbot, #2175/#2177)`, async () => {
      setBotsAvailableForTests(false);
      resetBotsPulseForTests();
      publishBots([{ id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" }]);
      const r = await mountRail({ paneOpen: false, favourites });
      expect(byLabel(r, "Tasks").length).toBe(1);
      expect(byLabel(r, "Bots").length).toBe(0);
      expect(r.root.findAll((n) => n.props?.className === "claude-rail-bots").length).toBe(0);
      resetBotsPulseForTests();
    });
  }

  test("the nav's order (owner, 2026-11-08): Search, Bots, Tasks, + at the rail's top; the Bots door is not at the foot", async () => {
    setLauncherAvailableForTests(true);
    setBotsAvailableForTests(true);
    const r = await mountRail({ paneOpen: false, favourites: true });
    expect(navOrder(r)).toEqual(["Search", "Bots", "Tasks", "New task"]);
    // One Bots door in all: the nav's.
    expect(byLabel(r, "Bots").length).toBe(1);
    expect(r.root.findAll((n) => n.props?.className === "sidebar-rail-items")[0]!.findAll((n) => n.props?.["aria-label"] === "Bots").length).toBe(0);
    setLauncherAvailableForTests(null);
  });

  test("no launcher panel in this process (a plain server): no Search door, the rest stand", async () => {
    setLauncherAvailableForTests(false);
    setBotsAvailableForTests(true);
    const r = await mountRail({ paneOpen: false, favourites: false });
    expect(navOrder(r)).toEqual(["Bots", "Tasks", "New task"]);
    setLauncherAvailableForTests(null);
  });

  test("the Search door shows the launcher panel (POST /api/launcher/show)", async () => {
    setLauncherAvailableForTests(true);
    setBotsAvailableForTests(true);
    const calls: string[] = [];
    (globalThis as { fetch: unknown }).fetch = (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${String(input)}`);
      return new Promise<Response>(() => {});
    };
    const r = await mountRail({ paneOpen: false, favourites: false });
    await act(async () => {
      byLabel(r, "Search")[0]!.props.onClick();
    });
    expect(calls).toContain("POST /api/launcher/show");
    setLauncherAvailableForTests(null);
  });

  test("pane open, favourites off: no glyph, no seam, the nav doors keep it standing, registered", async () => {
    const r = await mountRail({ paneOpen: true, favourites: false, chats: [chat("a")] });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(doors(r)).toBe(2);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open, favourites on, nothing to draw: the nav doors keep it standing", async () => {
    const r = await mountRail({ paneOpen: true, favourites: true });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(doors(r)).toBe(2);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open on its HOME, favourites on, a chat here: the list stands as beside any chat (2026-11-06; it stepped aside until then)", async () => {
    const r = await mountRail({ paneOpen: true, favourites: true, chats: [chat("a")] });
    expect(aside(r).props.hidden).toBeUndefined();
    expect(r.root.findAll((n) => n.type === "a" && typeof n.props["data-task-key"] === "string")).toHaveLength(1);
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open, favourites on, a chat here: standing and registered, still no glyph or seam", async () => {
    const r = await mountRail({ paneOpen: true, favourites: true, chats: [chat("a")], activeSessionId: chat("a").session_id });
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

  test("the ring of the chat the pane itself holds: its card pins too, the chat mounted in it (owner, 2026-11-08)", async () => {
    const opened: string[] = [];
    const b = chat("b");
    const release = announcePaneSession(b.session_id!);
    try {
      const r = await mountRail({
        paneOpen: true,
        favourites: true,
        chats: [chat("a"), b],
        onOpenChat: (h) => opened.push(h),
        activeSessionId: b.session_id,
      });
      await press(r, "b");
      expect(opened).toHaveLength(0);
      const peek = r.root.findAllByType(RailPeek);
      expect(peek).toHaveLength(1);
      expect(peek[0]!.props.pinned).toBe(true);
      expect(peek[0]!.props.shown.pulse.key).toBe(b.key);
      // The chat stands in the pane AND in the card (owner, 2026-11-08: "it
      // should work in window A as well"): a kept mount, not a note.
      expect(peek[0]!.props.kept.some((s: { pulse: { key: string } }) => s.pulse.key === b.key)).toBe(true);
      expect(peek[0]!.findAll((n) => n.props?.className === "claude-rail-peek-note").length).toBe(0);
    } finally {
      release();
    }
  });

  test("beside the open pane a ring press pins the card and leaves the pane's chat alone (owner, 2026-11-08)", async () => {
    const opened: string[] = [];
    const r = await mountRail({
      paneOpen: true,
      favourites: true,
      chats: [chat("a"), chat("b")],
      onOpenChat: (h) => opened.push(h),
      activeSessionId: chat("b").session_id,
    });
    await press(r, "a");
    expect(opened).toHaveLength(0);
    const peek = r.root.findAllByType(RailPeek);
    expect(peek).toHaveLength(1);
    expect(peek[0]!.props.pinned).toBe(true);
    expect(peek[0]!.props.shown.pulse.key).toBe(chat("a").key);
  });

});

describe("ClaudeRail's bot card (BotPeek; owner, 2026-11-05)", () => {
  const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
  const loc = (globalThis as unknown as { location: { pathname: string } }).location;
  let hadGetById = false;
  beforeEach(() => {
    installPortalContainer();
    (doc.body as { createNodeMock: unknown }).createNodeMock = () => fakeEl();
    hadGetById = "getElementById" in doc;
    doc.getElementById ??= () => null;
    resetBotsPulseForTests();
  });
  afterEach(() => {
    removePortalContainer();
    if (!hadGetById) delete doc.getElementById;
    resetBotsPulseForTests();
    loc.pathname = "/";
    // Nothing mounted here joins the shared prefs read, but a read that this
    // file's never-settling fetch stub pinned would stay pinned for every
    // later suite in the process (platform/lib/api): let it go regardless.
    dropSharedPrefsRead();
  });
  /** A pinned bot, as the pulse carries one (botsPulse `BotPulseRow`). */
  const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
  const ring = (r: ReactTestRenderer) => r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");
  const press = async (r: ReactTestRenderer) => {
    await act(async () => {
      ring(r).props.onClick({ preventDefault() {}, stopPropagation() {}, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, detail: 1, currentTarget: fakeEl() });
      await new Promise((res) => setTimeout(res, 0));
    });
    // The card's chat chunk (BotPeek's lazy BotChat) lands a tick later.
    await act(async () => {
      await new Promise((res) => setTimeout(res, 30));
    });
  };

  test("the pinned card is the shell's (owner, 2026-11-08): the page's rail remounting leaves it up — no close, no reopen — and the × clears the window's mark", async () => {
    setBotOpen(null);
    // The shell's host, mounted once (App), apart from the page's rail.
    let host!: ReactTestRenderer;
    await act(async () => {
      host = create(<BotCardHost />, { createNodeMock: () => fakeEl() });
    });
    mounted.push(host);
    const r = await mountRail({ paneOpen: false, favourites: true, botHost: false });
    await act(async () => {
      publishBots([BOT]);
    });
    await press(r);
    expect(botOpen()).toBe("b1");
    const peek = () => host.root.findByType(BotPeek);
    expect(peek().props.shown?.id).toBe("b1");
    // Held (favourites on): a press elsewhere on the page does not close it.
    expect(peek().props.held).toBe(true);
    // Every `shown` the host renders from here on, to catch a flicker.
    const seen: (string | null)[] = [];
    const watch = setInterval(() => seen.push(peek().props.shown?.id ?? null), 0);
    // The page's view switches: its rail unmounts and a fresh one mounts.
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    seen.push(peek().props.shown?.id ?? null);
    const r2 = await mountRail({ paneOpen: false, favourites: true, botHost: false });
    await act(async () => {
      await new Promise((res) => setTimeout(res, 10));
    });
    clearInterval(watch);
    seen.push(peek().props.shown?.id ?? null);
    expect(seen.every((k) => k === "b1")).toBe(true);
    expect(peek().props.pinned).toBe(true);
    // The new rail's ring reads it as its own.
    expect(r2.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1").props.className).toContain("is-pinned");
    const close = host.root.find((n) => n.type === "button" && n.props.className === "claude-rail-peek-btn claude-rail-peek-close");
    await act(async () => {
      close.props.onClick();
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(peek().props.shown).toBeNull();
    expect(botOpen()).toBeNull();
  });

  test("a reload on the Bots page pins the window's open bot again too (CodeRabbit on #2201)", async () => {
    setBotOpen("b1");
    loc.pathname = "/bots/b1";
    const r = await mountRail({ paneOpen: false, favourites: true });
    await act(async () => {
      publishBots([BOT]);
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown?.id).toBe("b1");
    expect(botOpen()).toBe("b1");
    setBotOpen(null);
  });

  test("a rail leaving lets go of a bot hover it held, and never of a pinned card (CodeRabbit on #2201)", async () => {
    const r = await mountRail({ paneOpen: false, favourites: true, botHost: false });
    await act(async () => {
      publishBots([BOT]);
    });
    const { botCardIntent } = await import("./bot-card-store");
    const intent = botCardIntent();
    const spy: string[] = [];
    const leave = intent.leaveRing.bind(intent);
    intent.leaveRing = (via) => {
      spy.push(via ?? "pointer");
      leave(via);
    };
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    expect(spy).toEqual(["pointer", "focus"]);
    intent.leaveRing = leave;
  });

  test("a reload pins the window's open bot again, once the pulse lists it", async () => {
    setBotOpen("b1");
    const r = await mountRail({ paneOpen: false, favourites: true });
    await act(async () => {
      publishBots([BOT]);
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown?.id).toBe("b1");
    expect(r.root.findByType(BotPeek).props.pinned).toBe(true);
    setBotOpen(null);
  });

  test("the window's open bot is forgotten where the rail cannot show it: a favourite's card holds window B", async () => {
    setBotOpen("b1");
    publishTaskFavourites({ enabled: true });
    const fav = { ...chat("f"), starred: true } as Task;
    setFavOpen(fav.key);
    const r = await mountRail({ paneOpen: false, favourites: true, chats: [fav] });
    await act(async () => {
      publishBots([BOT]);
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown).toBeNull();
    expect(botOpen()).toBeNull();
    setFavOpen(null);
  });

  test("a pinned bot's ring opens its card: a dialog's door, and the press shows the card on that bot", async () => {
    const r = await mountRail({ paneOpen: false, favourites: true });
    await act(async () => {
      publishBots([BOT]);
    });
    expect(ring(r).props["aria-haspopup"]).toBe("dialog");
    expect(ring(r).props["aria-expanded"]).toBe(false);
    // The card is the ring's caption while it can stand (an empty hint).
    expect(ring(r).props["data-hint"]).toBe("");
    // Mounted with the bots, closed: nothing shown yet.
    expect(r.root.findAllByType(BotPeek)).toHaveLength(1);
    expect(r.root.findByType(BotPeek).props.shown).toBeNull();
    await press(r);
    expect(r.root.findByType(BotPeek).props.shown?.id).toBe("b1");
    expect(r.root.findByType(BotPeek).props.pinned).toBe(true);
    expect(ring(r).props["aria-expanded"]).toBe(true);
    expect(ring(r).props["aria-controls"]).toBe("claude-rail-bot-peek");
    expect(ring(r).props.className).toContain("is-pinned");
    const chats = () => r.root.findAll((n) => n.type === "div" && n.props.className === "claude-rail-peek-chat is-shown");
    expect(chats()).toHaveLength(1);
    // The × closes the card; its chat leaves once the fade is done (BotPeek
    // CHAT_LINGER_MS; Bugbot on #2143) — nothing polls for a closed card.
    const close = r.root.find((n) => n.type === "button" && n.props.className === "claude-rail-peek-btn claude-rail-peek-close");
    await act(async () => {
      close.props.onClick();
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown).toBeNull();
    expect(chats()).toHaveLength(1);
    await act(async () => {
      await new Promise((res) => setTimeout(res, CHAT_LINGER_MS + 50));
    });
    expect(chats()).toHaveLength(0);
  });

  test("on the Bots page the ring is still the card's (2026-11-06): a popup, the card its caption; Open is the door", async () => {
    loc.pathname = "/bots/b1";
    const r = await mountRail({ paneOpen: false, favourites: true });
    await act(async () => {
      publishBots([BOT]);
    });
    expect(ring(r).props["aria-haspopup"]).toBe("dialog");
    expect(ring(r).props["data-hint"]).toBe("");
    expect(ring(r).props.href).toBe("/bots/b1");
  });
});

// THE GIT SECTION MOVES TO THE STATUS BAR (owner, 2026-11-07, the rail flag):
// with the nav doors the rail draws no Git section and publishes the page's
// repository for the status bar's Git chip (rail-git-store.ts, GitDock);
// flag off, the rail of before.
describe("ClaudeRail's git with the rail flag", () => {
  const REPO = {
    root: DIR,
    branch: "feature",
    has_remote: true,
    default_branch: "main",
    ahead: 0,
    behind: 2,
    commits: [{ sha: "abc1234def", short: "abc1234", subject: "Fix it", author: "me", when: 1_000 }],
  };
  beforeEach(() => {
    resetRailGitStoreForTests();
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail")
        ? Promise.resolve(new Response(JSON.stringify({ ok: true, repo: REPO }), { status: 200 }))
        : new Promise<Response>(() => {});
  });
  afterEach(() => resetRailGitStoreForTests());
  const gitGroup = (r: ReactTestRenderer) =>
    r.root.findAll((n) => typeof n.type === "string" && n.props.role === "group" && n.props["aria-label"] === "Git");

  test("flag on: no Git section on the rail; the repository and the Git panel door go to the store", async () => {
    let opened = 0;
    const r = await mountRail({ paneOpen: false, favourites: false, git: { path: DIR, onOpenGit: () => opened++ } });
    await act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(gitGroup(r)).toHaveLength(0);
    const door = railGitNow();
    expect(door?.repo.branch).toBe("feature");
    door?.onOpenGit?.();
    expect(opened).toBe(1);
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    expect(railGitNow()).toBeNull();
  });

  test("flag off: the rail's Git section as before, nothing published", async () => {
    const r = await mountRail({ paneOpen: false, favourites: false, peek: false, git: { path: DIR } });
    await act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(gitGroup(r)).toHaveLength(1);
    expect(railGitNow()).toBeNull();
  });
});
