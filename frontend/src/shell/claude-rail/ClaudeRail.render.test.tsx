// THE RAIL BESIDE THE OPEN PANE (ClaudeRail `paneOpen`; owner, 2026-11-05),
// mounted: no panel glyph (the nav doors stand there) and the pull seam only
// while the pane is shut; the rail stands beside the open pane too.
// Real modules throughout — the flags and the rows are published into their
// own stores (`mock.module` is process-wide in bun and leaks across files).
import { installDomShim, installPortalContainer, removePortalContainer } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestInstance, type ReactTestRenderer } from "react-test-renderer";
import type { Task, TaskPulseTask } from "@platform/lib/api";
import type { RailPlace } from "./claude-rail-lib";

const { ClaudeRail } = await import("./ClaudeRail");
const { RailPeek } = await import("./RailPeek");
const { publishTasks, rememberListing, forgetListing, resetListingFeedForTests } = await import("@shell/tasksPulse");
const { favPeekState, resetFavPeekStoreForTests } = await import("./fav-peek-store");
const { publishBots, resetBotsPulseForTests } = await import("@platform/lib/botsPulse");
const { setBotsAvailableForTests, setLauncherAvailableForTests } = await import("@platform/lib/bots-available");
const { announcePaneSession } = await import("@platform/lib/peek-session");
const { botOpen, setAPinned, setBotOpen, setFavOpen } = await import("@platform/lib/last-chat");
const { dropSharedPrefsRead } = await import("@platform/lib/api");
const { BotPeek, CHAT_LINGER_MS } = await import("./BotPeek");
const { BotCardHost } = await import("./BotCardHost");
const { botCardIntent, resetBotCardForTests } = await import("./bot-card-store");
const { railGitCount, railGitOf, resetRailGitStoreForTests } = await import("./rail-git-store");
const { gitCardRail, gitCardState, gitCardStore, registerGitCard, resetGitCardForTests } = await import("./git-peek-store");
const { resetRailGitCacheForTests } = await import("./rail-git");
const { GitPeek, GIT_FOLLOW_MS } = await import("./GitPeek");
const { modeCardStore, railModesOf, resetModeCardsForTests } = await import("./mode-peek-store");
const { publishRailModeDoors, resetRailModeDoorsFlagForTests } = await import("./rail-mode-doors-flag");
const { publishRailExpand, resetRailExpandFlagForTests } = await import("./rail-expand-flag");
const { RAIL_DEFAULT_WIDTH, getRailState, setRailState } = await import("@platform/lib/railstate");
const { _setUiLayoutForTest } = await import("@platform/lib/ui-layout");
const { DoorCard } = await import("./DoorCard");
const { PEEK_OPEN_MS } = await import("./rail-peek-lib");
const { botsCardState, registerBotsCard, resetBotsCardForTests } = await import("./bots-peek-store");
const { registerTasksCard, resetTasksCardForTests, tasksCardState } = await import("./tasks-peek-store");
const { resetFlagsForTests: resetClaudeFlagsForTests } = await import("@apps/claude/feature-flag");
const { TaskHead: TaskHeadC } = await import("@shell/TaskHead");

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
  setBotsAvailableForTests(null);
  (globalThis as { fetch: unknown }).fetch = realFetch;
  // The never-settling fetch above pins the shared prefs read (platform/lib/api)
  // for every later suite in the process once a mounted flag has joined it.
  dropSharedPrefsRead();
  resetClaudeFlagsForTests();
  // The expandable rail: its switch, and the rail collapsed again.
  resetRailExpandFlagForTests();
  setRailState({ width: RAIL_DEFAULT_WIDTH, expanded: false }, false);
  for (const [name, was] of saved) {
    if (was === MISSING) delete g[name];
    else g[name] = was;
  }
  saved.clear();
});

async function mountRail(o: {
  paneOpen: boolean;
  chats?: Task[];
  onOpenChat?: (href: string) => void;
  /** The session the page's URL names — beside the open pane, the chat it
   *  shows; null is the pane's home (2026-11-05, `listHidden`). */
  activeSessionId?: string | null;
  /** The page's git (ClaudeRail `git`). */
  git?: { path: string; onOpenGit?: () => void };
  /** The file's other modes (ClaudeRail `fileModes`). */
  fileModes?: { path: string; modes: string[]; onOpen?: (mode: string) => void };
  /** The mode doors' own switch (`rail_mode_doors_enabled`); on unless said. */
  modeDoors?: boolean;
  /** The expandable rail's switch (`rail_expand_enabled`); OFF unless said. */
  expand?: boolean;
  /** Mount the shell's bot card (BotCardHost) beside the rail, as App does. */
  botHost?: boolean;
  /** The rail's place; the demo folder unless said. */
  place?: RailPlace;
  onSelectNode?: (udfName: string | null) => void;
}) {
  publishRailModeDoors({ enabled: o.modeDoors ?? true });
  publishRailExpand({ enabled: o.expand ?? false });
  const rows = o.chats ?? [];
  rememberListing(rows);
  publishTasks(rows as unknown as TaskPulseTask[]);
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(
      <>
        <ClaudeRail
          paneOpen={o.paneOpen}
          place={o.place ?? { kind: "folder", dir: DIR }}
          {...(o.onSelectNode ? { onSelectNode: o.onSelectNode } : {})}
          newTask={{ target: DIR, waiting: null, on: null }}
          activeSessionId={o.activeSessionId ?? null}
          onOpen={() => {}}
          onOpenChat={o.onOpenChat ?? (() => {})}
          onPullStart={() => {}}
          {...(o.git ? { git: o.git } : {})}
          {...(o.fileModes
            ? {
                fileModes: {
                  path: o.fileModes.path,
                  doors: o.fileModes.modes.map((mode) => ({ mode, title: mode[0]!.toUpperCase() + mode.slice(1), icon: null })),
                  src: (mode: string) => "/explorer/embed" + o.fileModes!.path + "?_mode=" + mode,
                  onOpen: o.fileModes.onOpen ?? (() => {}),
                },
              }
            : {})}
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
// THE NAV DOORS (owner, 2026-11-07, order
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
  test("pane shut: Tasks and Bots doors in place of the glyph, seam, standing even empty, registered", async () => {
    const r = await mountRail({ paneOpen: false });
    expect(glyph(r)).toBe(0);
    expect(doors(r)).toBe(2);
    expect(seam(r)).toBe(1);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });

  test("a server without Bots (Windows): Tasks stands, no Bots door, no pinned bots (Bugbot, #2175/#2177)", async () => {
    setBotsAvailableForTests(false);
    resetBotsPulseForTests();
    publishBots([{ id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" }]);
    const r = await mountRail({ paneOpen: false });
    expect(byLabel(r, "Tasks").length).toBe(1);
    expect(byLabel(r, "Bots").length).toBe(0);
    expect(r.root.findAll((n) => n.props?.className === "claude-rail-bots").length).toBe(0);
    resetBotsPulseForTests();
  });

  test("the nav's order (owner, 2026-11-08): Search, Bots, Tasks, + at the rail's top; the Bots door is not at the foot", async () => {
    setLauncherAvailableForTests(true);
    setBotsAvailableForTests(true);
    const r = await mountRail({ paneOpen: false });
    expect(navOrder(r)).toEqual(["Search", "Bots", "Tasks", "New task"]);
    // One Bots door in all: the nav's.
    expect(byLabel(r, "Bots").length).toBe(1);
    expect(r.root.findAll((n) => n.props?.className === "sidebar-rail-items")[0]!.findAll((n) => n.props?.["aria-label"] === "Bots").length).toBe(0);
    setLauncherAvailableForTests(null);
  });

  test("no launcher panel in this process (a plain server): no Search door, the rest stand", async () => {
    setLauncherAvailableForTests(false);
    setBotsAvailableForTests(true);
    const r = await mountRail({ paneOpen: false });
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
    const r = await mountRail({ paneOpen: false });
    await act(async () => {
      byLabel(r, "Search")[0]!.props.onClick();
    });
    expect(calls).toContain("POST /api/launcher/show");
    setLauncherAvailableForTests(null);
  });

  test("pane open, favourites off: no glyph, no seam, the nav doors keep it standing, registered", async () => {
    const r = await mountRail({ paneOpen: true, chats: [chat("a")] });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(doors(r)).toBe(2);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open, favourites on, nothing to draw: the nav doors keep it standing", async () => {
    const r = await mountRail({ paneOpen: true });
    expect(glyph(r)).toBe(0);
    expect(seam(r)).toBe(0);
    expect(doors(r)).toBe(2);
    expect(aside(r).props.hidden).toBeUndefined();
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open on its HOME, favourites on, a chat here: the list stands as beside any chat (2026-11-06; it stepped aside until then)", async () => {
    const r = await mountRail({ paneOpen: true, chats: [chat("a")] });
    expect(aside(r).props.hidden).toBeUndefined();
    expect(r.root.findAll((n) => n.type === "a" && typeof n.props["data-task-key"] === "string")).toHaveLength(1);
    expect(favPeekState().rail).not.toBeNull();
  });

  test("pane open, favourites on, a chat here: standing and registered, still no glyph or seam", async () => {
    const r = await mountRail({ paneOpen: true, chats: [chat("a")], activeSessionId: chat("a").session_id });
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

  // THE SHARED TASK HEAD (shell/TaskHead; design.md "One task head,
  // everywhere"), behind its flag: the card's two-row head becomes the one
  // every task view draws — its own rename row gone, TaskPeekWho's in its place.
  const cls = (n: ReactTestInstance) => String(n.props?.className ?? "").split(" ");
  test("the card wears the shared head — the pin, the identity, the ⋮ — and no title row of its own", async () => {
    const r = await mountRail({ paneOpen: true, chats: [chat("a"), chat("b")], activeSessionId: chat("b").session_id });
    await press(r, "a");
    const peek = r.root.findByType(RailPeek);
    expect(peek.findAll((n) => typeof n.type === "string" && cls(n).includes("task-head"))).toHaveLength(1);
    expect(peek.findAll((n) => cls(n).includes("claude-rail-peek-titlerow"))).toHaveLength(0);
    expect(peek.findAll((n) => typeof n.type === "string" && cls(n).includes("task-side-peek-who")).length).toBeGreaterThan(0);
    // With the unified head the rail card has NO × (owner, 2026-11-16: a
    // click outside closes a temp card, a pinned one is unpinned first):
    // the pin is the head's first button.
    expect(peek.findAll((n) => typeof n.type === "string" && n.props["aria-label"] === "Close the preview")).toHaveLength(0);
    expect(peek.findAll((n) => typeof n.type === "string" && cls(n).includes("rail-pin-btn"))).toHaveLength(1);
    // The card's OWN star (toggleStar: tells the rail, dismisses on unstar),
    // not the head's plain TaskStar, as the count slot's other occupant —
    // and its archive as the id slot's (the a/b slots, 2026-11-15).
    expect(peek.findAll((n) => typeof n.type === "string" && cls(n).includes("task-star"))).toHaveLength(0);
    // A rail window seats it in the acts' layer of its facts box (the count
    // and the time are the other layer; 2026-11-16).
    const acts = peek.find((n) => typeof n.type === "string" && cls(n).includes("task-head-facts-layer") && cls(n).includes("is-b"));
    expect(acts.findAll((n) => typeof n.type === "string" && cls(n).includes("claude-rail-peek-star"))).toHaveLength(1);
    const idSlot = peek.find((n) => typeof n.type === "string" && cls(n).includes("task-head-idslot"));
    expect(idSlot.findAll((n) => typeof n.type === "string" && cls(n).includes("claude-rail-peek-archive")).length).toBe(1);
    // The status icon trades with nothing.
    const ring = peek.find((n) => typeof n.type === "string" && n.props.className === "task-side-peek-status");
    expect(ring.findAll((n) => typeof n.type === "string" && cls(n).includes("claude-rail-peek-archive")).length).toBe(0);
    // Keyed per chat, so a half-typed rename cannot follow a swap.
    expect(peek.findByType(TaskHeadC).props.task.key).toBe(chat("a").key);
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

  test("a reload on the Bots page pins the window's open bot again too (CodeRabbit on #2201)", async () => {
    setBotOpen("b1");
    loc.pathname = "/bots/b1";
    const r = await mountRail({ paneOpen: false });
    await act(async () => {
      publishBots([BOT]);
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown?.id).toBe("b1");
    expect(botOpen()).toBe("b1");
    setBotOpen(null);
  });

  test("a rail leaving lets go of a bot hover it held, and never of a pinned card (CodeRabbit on #2201)", async () => {
    const r = await mountRail({ paneOpen: false, botHost: false });
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
    const r = await mountRail({ paneOpen: false });
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
    const fav = { ...chat("f"), starred: true } as Task;
    setFavOpen(fav.key);
    const r = await mountRail({ paneOpen: false, chats: [fav] });
    await act(async () => {
      publishBots([BOT]);
      await new Promise((res) => setTimeout(res, 0));
    });
    expect(r.root.findByType(BotPeek).props.shown).toBeNull();
    expect(botOpen()).toBeNull();
    setFavOpen(null);
  });

  test("a pinned bot's ring opens its card: a dialog's door, and the press shows the card on that bot", async () => {
    const r = await mountRail({ paneOpen: false });
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
    // The card closes; its chat leaves once the fade is done (BotPeek
    // CHAT_LINGER_MS; Bugbot on #2143) — nothing polls for a closed card.
    await act(async () => {
      botCardIntent().dismiss();
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
    const r = await mountRail({ paneOpen: false });
    await act(async () => {
      publishBots([BOT]);
    });
    expect(ring(r).props["aria-haspopup"]).toBe("dialog");
    expect(ring(r).props["data-hint"]).toBe("");
    expect(ring(r).props.href).toBe("/bots/b1");
  });
});

describe("ClaudeRail's Bots door and its card (BotsPeek; owner, 2026-11-09)", () => {
  beforeEach(() => {
    resetBotsCardForTests();
    resetTasksCardForTests();
  });
  afterEach(() => {
    resetBotsCardForTests();
    resetTasksCardForTests();
  });
  const door = (r: ReactTestRenderer, label: string) => byLabel(r, label)[0]!;
  const press = (r: ReactTestRenderer, label: string) =>
    act(() => {
      door(r, label).props.onClick({ preventDefault() {}, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, detail: 1 });
    });

  test("no card mounted (an embed): the Bots door is the plain link, no popup", async () => {
    const r = await mountRail({ paneOpen: false });
    expect(door(r, "Bots").props["aria-haspopup"]).toBeUndefined();
    expect(door(r, "Bots").props.href).toBe("/bots");
  });

  test("a card mounted: the door is a dialog's; a press pins the card, a second press closes it (the Tasks door's toggle)", async () => {
    const off = registerBotsCard();
    const r = await mountRail({ paneOpen: false });
    expect(door(r, "Bots").props["aria-haspopup"]).toBe("dialog");
    expect(door(r, "Bots").props["aria-expanded"]).toBe(false);
    await press(r, "Bots");
    expect(botsCardState()).toMatchObject({ up: true, pinned: true });
    expect(door(r, "Bots").props["aria-expanded"]).toBe(true);
    await press(r, "Bots");
    expect(botsCardState().up).toBe(false);
    off();
  });

  test("one window A card at a time: the Tasks door's press puts the Bots card down, and the Bots door's the Tasks card", async () => {
    const offB = registerBotsCard();
    const offT = registerTasksCard();
    const r = await mountRail({ paneOpen: false });
    await press(r, "Bots");
    expect(botsCardState().up).toBe(true);
    await press(r, "Tasks");
    expect(tasksCardState().up).toBe(true);
    expect(botsCardState().up).toBe(false);
    await press(r, "Bots");
    expect(botsCardState().up).toBe(true);
    expect(tasksCardState().up).toBe(false);
    offB();
    offT();
  });
});

// THE FILE'S OTHER MODES AS DOORS (owner, 2026-11-20, the rail flag): above
// the Git door and in its group, one door per mode the page hands, each with
// its own window A card (mode-peek-store, ModePeek) — the Git door's rules.
describe("ClaudeRail's mode doors (owner, 2026-11-20)", () => {
  const FILE = DIR + "/index.html";
  const REPO = {
    root: DIR,
    branch: "main",
    has_remote: false,
    default_branch: "main",
    ahead: 0,
    behind: 0,
    upstream: null,
    commits: [],
  };
  let repo: unknown = null;
  const reset = () => {
    resetModeCardsForTests();
    resetRailModeDoorsFlagForTests();
    resetRailGitCacheForTests();
    resetRailGitStoreForTests();
    resetGitCardForTests();
    resetBotsCardForTests();
    resetTasksCardForTests();
  };
  beforeEach(() => {
    repo = null;
    reset();
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail")
        ? Promise.resolve(new Response(JSON.stringify({ ok: true, repo }), { status: 200 }))
        : new Promise<Response>(() => {});
  });
  afterEach(reset);
  const settle = () =>
    act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });
  const doors = (r: ReactTestRenderer) => r.root.findAll((n) => n.type === "button" && n.props["data-rail-door"] === "mode");
  const items = (r: ReactTestRenderer): string[] =>
    r.root
      .find((n) => n.props?.className === "sidebar-rail-items")
      .children.filter((c): c is ReactTestInstance => typeof c !== "string")
      .map((c) => String(c.props.className ?? ""));
  const press = (r: ReactTestRenderer, mode: string) =>
    act(() => {
      doors(r)
        .find((d) => d.props["data-rail-mode"] === mode)!
        .props.onClick({ preventDefault() {}, button: 0, detail: 1 });
    });

  test("one door per mode, in the page's order, past one hairline; the facts by rail", async () => {
    const r = await mountRail({ paneOpen: false, fileModes: { path: FILE, modes: ["code", "reader"] } });
    await settle();
    expect(doors(r).map((d) => [d.props["data-rail-mode"], d.props["aria-label"], d.props["data-hint"]])).toEqual([
      ["code", "Code", "Code"],
      ["reader", "Reader", "Reader"],
    ]);
    expect(items(r).slice(0, 2)).toEqual(["sidebar-rail-sep is-tight", "claude-rail-nav claude-rail-mode-doors"]);
    const facts = railModesOf(favPeekState().rail);
    expect(facts?.path).toBe(FILE);
    expect(facts?.src("code")).toBe("/explorer/embed" + FILE + "?_mode=code");
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    expect(railModesOf(favPeekState().rail)).toBeNull();
  });

  test("with Git: the mode doors, then the Git door, one hairline for the group", async () => {
    repo = REPO;
    const r = await mountRail({
      paneOpen: false,
      git: { path: FILE },
      fileModes: { path: FILE, modes: ["code"] },
    });
    await settle();
    expect(items(r).slice(0, 3)).toEqual([
      "sidebar-rail-sep is-tight",
      "claude-rail-nav claude-rail-mode-doors",
      "claude-rail-nav claude-rail-git-door",
    ]);
  });

  test("their own switch off: no door, no hairline, nothing published", async () => {
    const r = await mountRail({
      paneOpen: false,
      modeDoors: false,
      fileModes: { path: FILE, modes: ["code", "reader"] },
    });
    await settle();
    expect(doors(r)).toHaveLength(0);
    expect(items(r)).not.toContain("sidebar-rail-sep is-tight");
    expect(railModesOf(favPeekState().rail)).toBeNull();
  });

  test("no modes: no door, no hairline, nothing published", async () => {
    const none = await mountRail({ paneOpen: false, fileModes: { path: FILE, modes: [] } });
    await settle();
    expect(doors(none)).toHaveLength(0);
    expect(items(none)).not.toContain("sidebar-rail-sep is-tight");
    expect(railModesOf(favPeekState().rail)).toBeNull();
  });

  test("no card behind the doors: a press is the page's own switch", async () => {
    const opened: string[] = [];
    const r = await mountRail({
      paneOpen: false,
      fileModes: { path: FILE, modes: ["code", "reader"], onOpen: (m) => opened.push(m) },
    });
    await settle();
    press(r, "reader");
    expect(opened).toEqual(["reader"]);
    expect(modeCardStore("reader").state().up).toBe(false);
  });

  const up = (mode: string) => modeCardStore(mode).state().up;

  test("a card behind each: a press pins that mode's, the same door's next press closes it", async () => {
    const offs = [modeCardStore("code").registerCard(), modeCardStore("reader").registerCard()];
    const r = await mountRail({ paneOpen: false, fileModes: { path: FILE, modes: ["code", "reader"] } });
    await settle();
    press(r, "code");
    expect(modeCardStore("code").state()).toMatchObject({ up: true, pinned: true });
    expect(doors(r).map((d) => String(d.props.className).includes("is-open"))).toEqual([true, false]);
    expect(doors(r).map((d) => d.props["aria-controls"])).toEqual(["claude-rail-mode-peek-code", "claude-rail-mode-peek-reader"]);
    press(r, "code");
    expect(up("code")).toBe(false);
    for (const off of offs) off();
  });

  test("another mode's door opens ITS card and puts the first one down (one non-pinned window A card)", async () => {
    const offs = [modeCardStore("code").registerCard(), modeCardStore("reader").registerCard()];
    const r = await mountRail({ paneOpen: false, fileModes: { path: FILE, modes: ["code", "reader"] } });
    await settle();
    press(r, "code");
    press(r, "reader");
    expect([up("code"), up("reader")]).toEqual([false, true]);
    expect(doors(r).map((d) => String(d.props.className).includes("is-open"))).toEqual([false, true]);
    for (const off of offs) off();
  });

  test("another window A card coming puts it down, and it puts that one down", async () => {
    repo = REPO;
    const offMode = modeCardStore("code").registerCard();
    const offGit = registerGitCard();
    const r = await mountRail({
      paneOpen: false,
      git: { path: FILE },
      fileModes: { path: FILE, modes: ["code"] },
    });
    await settle();
    press(r, "code");
    expect(up("code")).toBe(true);
    const git = r.root.find((n) => n.type === "button" && n.props["data-rail-door"] === "git");
    act(() => {
      git.props.onClick({ preventDefault() {}, button: 0, detail: 1 });
    });
    expect(gitCardState().up).toBe(true);
    expect(up("code")).toBe(false);
    press(r, "code");
    expect(up("code")).toBe(true);
    expect(gitCardState().up).toBe(false);
    offMode();
    offGit();
  });
});

// THE GIT SECTION BECOMES A DOOR (owner, 2026-11-10, the rail flag): under
// the rings, a hairline and one Git door seat; the page's repository goes to
// the shell's Git card by rail (rail-git-store.ts, GitPeek). Flag off, the
// rail's Git section of before.
describe("ClaudeRail's Git door (owner, 2026-11-10)", () => {
  const REPO = {
    root: DIR,
    branch: "feature",
    has_remote: true,
    default_branch: "main",
    ahead: 0,
    behind: 2,
    upstream: { ahead: 1, behind: 0 },
    commits: ["a", "b", "c", "d", "e"].map((k) => ({ sha: `${k}234def`, short: `${k}234`, subject: `Fix ${k}`, author: "me", when: 1_000 })),
  };
  let answer: unknown = REPO;
  beforeEach(() => {
    answer = REPO;
    resetRailGitCacheForTests();
    resetRailGitStoreForTests();
    resetGitCardForTests();
    resetBotsCardForTests();
    resetTasksCardForTests();
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail")
        ? Promise.resolve(new Response(JSON.stringify({ ok: true, repo: answer }), { status: 200 }))
        : new Promise<Response>(() => {});
  });
  afterEach(() => {
    resetRailGitStoreForTests();
    resetGitCardForTests();
    resetBotsCardForTests();
    resetTasksCardForTests();
    // A pinned card's window mark (last-chat `aPinned`) would pin it back on
    // the next test's rail.
    setAPinned(null);
  });
  const settle = () =>
    act(async () => {
      await new Promise((res) => setTimeout(res, 0));
    });
  const gitGroup = (r: ReactTestRenderer) =>
    r.root.findAll((n) => typeof n.type === "string" && n.props.role === "group" && n.props["aria-label"] === "Git");
  const gitDoor = (r: ReactTestRenderer) =>
    r.root.findAll((n) => n.type === "button" && typeof n.props["aria-label"] === "string" && n.props["aria-label"].startsWith("Git, on "));
  /** The items column's children, by class: where the hairline and the door stand. */
  const items = (r: ReactTestRenderer): string[] =>
    r.root
      .find((n) => n.props?.className === "sidebar-rail-items")
      .children.filter((c): c is ReactTestInstance => typeof c !== "string")
      .map((c) => String(c.props.className ?? ""));
  const pressGit = (r: ReactTestRenderer) =>
    act(() => {
      gitDoor(r)[0]!.props.onClick({ preventDefault() {}, button: 0, detail: 1 });
    });

  test("no rings: the hairline then the Git door; its words, dots and facts by rail", async () => {
    let opened = 0;
    const r = await mountRail({ paneOpen: false, git: { path: DIR, onOpenGit: () => opened++ } });
    await settle();
    expect(gitGroup(r)).toHaveLength(0);
    expect(items(r).slice(0, 2)).toEqual(["sidebar-rail-sep is-tight", "claude-rail-nav claude-rail-git-door"]);
    const door = gitDoor(r)[0]!;
    // The app's instant caption beside the door, not the browser's `title`
    // (owner, 2026-11-18). No card host here, so the hover brings no card
    // up and the door says its words.
    expect(door.props.title).toBeUndefined();
    expect(door.props["data-hint-side"]).toBe("left");
    expect(door.props["data-hint"]).toBe("Git — on feature · 0 ahead · 2 behind main · 1 to push · 2 to pull");
    expect(door.props["aria-label"]).toBe("Git, on feature, 1 to push, 2 to pull, updates on the remote");
    // Green on top (2 behind main), the push dot bottom-right.
    const dots = door.findAll((n) => typeof n.props?.className === "string" && n.props.className.startsWith("sidebar-rail-dot"));
    expect(dots.map((d) => d.props.className)).toEqual(["sidebar-rail-dot is-unread", "sidebar-rail-dot is-push is-second"]);
    const facts = railGitOf(favPeekState().rail);
    expect(facts?.repo.branch).toBe("feature");
    facts?.onOpenGit?.();
    expect(opened).toBe(1);
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    expect(railGitCount()).toBe(0);
  });

  test("with rings: the hairline and the door stand under the list", async () => {
    const r = await mountRail({ paneOpen: false, chats: [chat("a")], git: { path: DIR } });
    await settle();
    const at = items(r);
    const list = at.indexOf("claude-rail-chats");
    expect(list).toBeGreaterThanOrEqual(0);
    expect(at.slice(list + 1, list + 3)).toEqual(["sidebar-rail-sep is-tight", "claude-rail-nav claude-rail-git-door"]);
  });

  test("no repository, or no git on the page: no door, no hairline, nothing published", async () => {
    answer = null;
    const r = await mountRail({ paneOpen: false, git: { path: DIR } });
    await settle();
    expect(gitDoor(r)).toHaveLength(0);
    expect(items(r)).not.toContain("sidebar-rail-sep is-tight");
    expect(railGitCount()).toBe(0);
    act(() => r.unmount());
    mounted.splice(mounted.indexOf(r), 1);
    const bare = await mountRail({ paneOpen: false });
    await settle();
    expect(gitDoor(bare)).toHaveLength(0);
  });

  test("no card to stand: the press opens the page's Git panel; a card mounted: pin, then the second press closes", async () => {
    let opened = 0;
    const r = await mountRail({ paneOpen: false, git: { path: DIR, onOpenGit: () => opened++ } });
    await settle();
    expect(gitDoor(r)[0]!.props["aria-haspopup"]).toBeUndefined();
    await pressGit(r);
    expect(opened).toBe(1);
    expect(gitCardState().up).toBe(false);
    let off = () => {};
    act(() => {
      off = registerGitCard();
    });
    expect(gitDoor(r)[0]!.props["aria-haspopup"]).toBe("dialog");
    await pressGit(r);
    expect(gitCardState()).toMatchObject({ up: true, pinned: true });
    expect(gitDoor(r)[0]!.props["aria-expanded"]).toBe(true);
    expect(gitDoor(r)[0]!.props.className).toContain("is-open is-pinned");
    await pressGit(r);
    expect(gitCardState().up).toBe(false);
    expect(opened).toBe(1);
    act(() => off());
  });

  test("one window A card at a time: Git and Bots put each other down", async () => {
    const offG = registerGitCard();
    const offB = registerBotsCard();
    const r = await mountRail({ paneOpen: false, git: { path: DIR } });
    await settle();
    await pressGit(r);
    expect(gitCardState().up).toBe(true);
    await act(() => {
      byLabel(r, "Bots")[0]!.props.onClick({ preventDefault() {}, button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false, detail: 1 });
    });
    expect(botsCardState().up).toBe(true);
    expect(gitCardState().up).toBe(false);
    await pressGit(r);
    expect(gitCardState().up).toBe(true);
    expect(botsCardState().up).toBe(false);
    act(() => {
      offG();
      offB();
    });
  });

  test("an explorer split: the press records ITS rail, whose facts the card reads; that rail going forgets it (owner, 2026-11-10)", async () => {
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail")
        ? Promise.resolve(
            new Response(JSON.stringify({ ok: true, repo: { ...REPO, branch: String(url).includes("left") ? "left" : "right" } }), { status: 200 }),
          )
        : new Promise<Response>(() => {});
    let off = () => {};
    act(() => {
      off = registerGitCard();
    });
    const left = await mountRail({ paneOpen: false, git: { path: `${DIR}/left` } });
    const right = await mountRail({ paneOpen: false, git: { path: `${DIR}/right` } });
    await settle();
    expect(railGitCount()).toBe(2);
    // The top rail is the right one; the press is on the left one's door.
    expect(railGitOf(favPeekState().rail)?.repo.branch).toBe("right");
    await pressGit(left);
    // Pinned: the press, then its pin (a press alone is a temp card).
    act(() => gitCardStore.setHeld(true));
    expect(gitCardState()).toMatchObject({ up: true, pinned: true, held: true });
    expect(railGitOf(gitCardRail())?.repo.branch).toBe("left");
    // The left rail goes: the pinned card stays, beside the top rail now.
    act(() => left.unmount());
    mounted.splice(mounted.indexOf(left), 1);
    expect(gitCardRail()).toBeNull();
    expect(gitCardState().up).toBe(true);
    expect(gitDoor(right)).toHaveLength(1);
    act(() => off());
  });

  test("a same-repo hop mounts a NEW rail in one commit: the door never goes, and the pinned card follows (QA, 2026-11-10)", async () => {
    installPortalContainer();
    const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
    (doc.body as { createNodeMock: unknown }).createNodeMock = () => fakeEl();
    const hadGetById = "getElementById" in doc;
    doc.getElementById ??= () => null;
    const FILE = `${DIR}/f.py`;
    // The file's own read never answers: the first frame is the cache's.
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail") && !String(url).includes("f.py")
        ? Promise.resolve(new Response(JSON.stringify({ ok: true, repo: REPO }), { status: 200 }))
        : new Promise<Response>(() => {});
    try {
      const Hop = ({ at }: { at: string }) => (
        <>
          <ClaudeRail
            key={at}
            paneOpen={false}
            place={{ kind: "folder", dir: DIR }}
            newTask={{ target: DIR, waiting: null, on: null }}
            activeSessionId={null}
            onOpen={() => {}}
            onOpenChat={() => {}}
            onPullStart={() => {}}
            git={{ path: at, onOpenGit: () => {} }}
          />
          <GitPeek />
        </>
      );
      let r!: ReactTestRenderer;
      await act(async () => {
        r = create(<Hop at={DIR} />, { createNodeMock: () => fakeEl() });
      });
      mounted.push(r);
      await settle();
      expect(gitCardState().ready).toBe(true);
      await pressGit(r);
      // Pinned: the press, then its pin (a press alone is a temp card).
      act(() => gitCardStore.setHeld(true));
      expect(gitCardState()).toMatchObject({ up: true, pinned: true, held: true });
      // The hop: rail A unmounts, rail B mounts, one commit.
      act(() => r.update(<Hop at={FILE} />));
      expect(gitDoor(r)).toHaveLength(1);
      expect(items(r)).toContain("sidebar-rail-sep is-tight");
      expect(gitCardState()).toMatchObject({ up: true, pinned: true });
      await settle();
      expect(gitDoor(r)).toHaveLength(1);
      expect(gitCardState()).toMatchObject({ up: true, pinned: true });
      // Beside the new rail, on its (seeded) repository.
      const card = r.root.findByType(DoorCard);
      expect(card.props.rail).toBe(favPeekState().rail);
      expect(railGitOf(card.props.rail)?.repo.branch).toBe("feature");
      // …and still there once the follow window is over.
      await act(async () => {
        await new Promise((res) => setTimeout(res, GIT_FOLLOW_MS + 50));
      });
      expect(gitCardState()).toMatchObject({ up: true, pinned: true });
    } finally {
      for (const m of mounted.splice(0)) act(() => m.unmount());
      removePortalContainer();
      if (!hadGetById) delete doc.getElementById;
    }
  });

  test("a split whose TOP rail has no repository: the other rail's door still previews and pins, the card on its repo (Bugbot on #2236)", async () => {
    installPortalContainer();
    const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
    (doc.body as { createNodeMock: unknown }).createNodeMock = () => fakeEl();
    const hadGetById = "getElementById" in doc;
    doc.getElementById ??= () => null;
    (globalThis as { fetch: unknown }).fetch = (url: string) =>
      String(url).includes("/api/git/rail")
        ? Promise.resolve(
            new Response(JSON.stringify({ ok: true, repo: String(url).includes("left") ? { ...REPO, branch: "left" } : null }), { status: 200 }),
          )
        : new Promise<Response>(() => {});
    try {
      const rail = (at: string) => (
        <ClaudeRail
          key={at}
          paneOpen={false}
          place={{ kind: "folder", dir: DIR }}
          newTask={{ target: DIR, waiting: null, on: null }}
          activeSessionId={null}
          onOpen={() => {}}
          onOpenChat={() => {}}
          onPullStart={() => {}}
          git={{ path: at }}
        />
      );
      let r!: ReactTestRenderer;
      await act(async () => {
        r = create(
          <>
            {rail(`${DIR}/left`)}
            {rail(`${DIR}/right`)}
            <GitPeek />
          </>,
          { createNodeMock: () => fakeEl() },
        );
      });
      mounted.push(r);
      await settle();
      // The top rail (the right, mounted last) has no repository; the left has.
      expect(railGitOf(favPeekState().rail)).toBeNull();
      expect(gitDoor(r)).toHaveLength(1);
      expect(gitCardState().ready).toBe(true);
      expect(gitDoor(r)[0]!.props["aria-haspopup"]).toBe("dialog");
      // A hover previews…
      await act(async () => {
        gitDoor(r)[0]!.props.onPointerEnter({ pointerType: "mouse", buttons: 0 });
        await new Promise((res) => setTimeout(res, PEEK_OPEN_MS + 20));
      });
      expect(gitCardState()).toMatchObject({ up: true, pinned: false });
      // …and a press pins it, beside the left rail, on its repository.
      await pressGit(r);
      expect(gitCardState()).toMatchObject({ up: true, pinned: true });
      const card = r.root.findByType(DoorCard);
      expect(card.props.rail).toBe(gitCardRail());
      expect(railGitOf(card.props.rail)?.repo.branch).toBe("left");
      expect(card.findAll((n) => n.props?.className === "git-panel-branch-name")[0]!.children).toEqual(["left"]);
    } finally {
      for (const m of mounted.splice(0)) act(() => m.unmount());
      removePortalContainer();
      if (!hadGetById) delete doc.getElementById;
    }
  });

});

describe("ClaudeRail on a canvas with a node selected (owner, 2026-11-10)", () => {
  const CANVAS: RailPlace = { kind: "canvas", env: "prod", name: "demo", dir: DIR };
  const chats = [
    chat("other", { target: `${DIR}/o.py`, happened_at: 3_000, last_active: 3_000 }),
    chat("mine", { target: `${DIR}/n.py`, happened_at: 1_000 }),
  ];
  const seatKeys = (r: ReactTestRenderer) =>
    r.root
      .findAll((n) => typeof n.type === "string" && n.props.role === "listitem")
      .map((n) => [n.findAll((c) => typeof c.props?.["data-task-key"] === "string")[0]!.props["data-task-key"], n.props.className]);
  const seps = (r: ReactTestRenderer) => r.root.findAll((n) => n.props?.className === "claude-rail-node-sep").length;

  test("every canvas chat stays — the node's first, a hairline, the rest faded — and no way-back seat", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: { ...CANVAS, file: "n.py" }, onSelectNode: () => {} });
    expect(seatKeys(r)).toEqual([
      ["mine", "claude-rail-seat"],
      ["other", "claude-rail-seat is-off"],
    ]);
    expect(seps(r)).toBe(1);
    expect(r.root.findAll((n) => typeof n.props?.className === "string" && n.props.className.includes("claude-rail-all")).length).toBe(0);
  });

  test("no node: the List's order, nothing faded, no hairline", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: CANVAS, onSelectNode: () => {} });
    expect(seatKeys(r)).toEqual([
      ["other", "claude-rail-seat"],
      ["mine", "claude-rail-seat"],
    ]);
    expect(seps(r)).toBe(0);
  });

  test("a node with no chat of its own: every chat faded, no hairline above them", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: { ...CANVAS, file: "x.py" }, onSelectNode: () => {} });
    expect(seatKeys(r).map(([, c]) => c)).toEqual(["claude-rail-seat is-off", "claude-rail-seat is-off"]);
    expect(seps(r)).toBe(0);
  });
});

describe("ClaudeRail on an open file (owner, 2026-11-16)", () => {
  const FILE = `${DIR}/n.py`;
  const chats = [
    chat("folder", { happened_at: 3_000, last_active: 3_000 }),
    chat("other", { target: `${DIR}/o.py`, happened_at: 2_000, last_active: 2_000 }),
    chat("mine", { target: FILE, happened_at: 1_000 }),
  ];
  const seatKeys = (r: ReactTestRenderer) =>
    r.root
      .findAll((n) => typeof n.type === "string" && n.props.role === "listitem")
      .map((n) => [n.findAll((c) => typeof c.props?.["data-task-key"] === "string")[0]!.props["data-task-key"], n.props.className]);
  const seps = (r: ReactTestRenderer) => r.root.findAll((n) => n.props?.className === "claude-rail-node-sep").length;

  test("the folder's chats stay — the file's first, a hairline, the rest faded", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: { kind: "file", file: FILE, dir: DIR } });
    expect(seatKeys(r)).toEqual([
      ["mine", "claude-rail-seat"],
      ["folder", "claude-rail-seat is-off"],
      ["other", "claude-rail-seat is-off"],
    ]);
    expect(seps(r)).toBe(1);
  });

  test("the folder page itself: the List's order, nothing faded, no hairline", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: { kind: "folder", dir: DIR } });
    expect(seatKeys(r)).toEqual([
      ["folder", "claude-rail-seat"],
      ["other", "claude-rail-seat"],
      ["mine", "claude-rail-seat"],
    ]);
    expect(seps(r)).toBe(0);
  });

  test("a file with no chat of its own: the folder's chats faded, no hairline above them", async () => {
    const r = await mountRail({ paneOpen: false, chats, place: { kind: "file", file: `${DIR}/x.py`, dir: DIR } });
    expect(seatKeys(r).map(([, c]) => c)).toEqual(["claude-rail-seat is-off", "claude-rail-seat is-off", "claude-rail-seat is-off"]);
    expect(seps(r)).toBe(0);
  });
});

describe("ClaudeRail's ring hover: the task's row, not a title tooltip (owner, 2026-11-16)", () => {
  const ringsOf = (r: ReactTestRenderer, key: string) =>
    r.root.findAll((n) => n.type === "a" && n.props["data-task-key"] === key);
  const inFoot = (n: ReactTestInstance) => {
    for (let up = n.parent; up; up = up.parent) if (up.props?.className === "claude-rail-favs") return true;
    return false;
  };
  const row = chat("a", { task_id: "TASK-042", title: "Rename the pane noun", message_count: 7, last_reply: "Done — renamed in three files." });
  // The row's left part, gone since 2026-11-17 ("strip the left part, that
  // is status and task id").
  // …and the window's width, gone since 2026-11-18 ("make it not the same
  // width as the window"): the row is as wide as its words.
  const GONE_ATTRS = ["data-hint-id", "data-hint-width", "data-hint-width-of"];

  test("a list ring carries the title, count, time and window A's width — no number; the rail names the edge", async () => {
    const r = await mountRail({ paneOpen: false, chats: [row] });
    const [ring] = ringsOf(r, "a");
    expect(ring).toBeDefined();
    // Opted into the row form (platform/lib/hints `renderRowHint`)…
    expect(ring!.props["data-hint-row"]).toBe("");
    // …with the title, the count and the time (2026-11-17: "strip the left
    // part, that is status and task id, keep the time and count as is"):
    // no number is handed over any more.
    expect(ring!.props["data-hint-title"]).toBe("Rename the pane noun");
    expect(ring!.props["data-hint-count"]).toBe("7");
    expect(String(ring!.props["data-hint-when"]).length).toBeGreaterThan(0);
    for (const a of GONE_ATTRS) expect(a in ring!.props).toBe(false);
    // …and the window it opens, the one the scrim dims: A.
    expect(ring!.props["data-hint-window"]).toBe("a");
    expect(ring!.props["data-hint-side"]).toBe("left");
    // The row stands against the rail's own edge, where its windows do.
    expect(aside(r).props["data-hint-edge"]).toBe("");
  });

  test("a favourite's ring names window B, the list's ring window A — and neither asks for a width", async () => {
    const fav = { ...row, starred: true } as Task;
    const r = await mountRail({ paneOpen: false, chats: [fav] });
    const rings = ringsOf(r, "a");
    const foot = rings.filter(inFoot);
    const list = rings.filter((n) => !inFoot(n));
    expect(foot).toHaveLength(1);
    expect(foot[0]!.props["data-hint-row"]).toBe("");
    // It names window B as the one its row dims; the list's rings name A.
    expect(foot[0]!.props["data-hint-window"]).toBe("b");
    for (const ring of list) expect(ring.props["data-hint-window"]).toBe("a");
    for (const ring of [...foot, ...list]) for (const a of GONE_ATTRS) expect(a in ring.props).toBe(false);
  });

  test("the open favourite's ring says nothing, like window A's open ring; the other favourites keep their row (2026-11-17)", async () => {
    const open = { ...row, starred: true } as Task;
    const other = { ...chat("b", { task_id: "TASK-043", title: "Another favourite" }), starred: true } as Task;
    setFavOpen(open.key);
    try {
      const r = await mountRail({ paneOpen: false, chats: [open, other] });
      const mine = ringsOf(r, open.key).find(inFoot);
      const theirs = ringsOf(r, other.key).find(inFoot);
      expect(mine).toBeDefined();
      expect(theirs).toBeDefined();
      // The empty hint is the opt-out (hints.ts `hintOf`): no row, no tooltip.
      expect(mine!.props["data-hint"]).toBe("");
      expect(theirs!.props["data-hint"]).toBe("Another favourite");
      expect(theirs!.props["data-hint-row"]).toBe("");
    } finally {
      setFavOpen(null);
    }
  });

  test("a chat with no count prints none — the seat is left out, not zeroed", async () => {
    const r = await mountRail({ paneOpen: false, chats: [chat("a", { message_count: 0 })] });
    const [ring] = ringsOf(r, "a");
    expect(ring!.props["data-hint-row"]).toBe("");
    expect(ring!.props["data-hint-count"]).toBe("");
  });

});

// THE EXPANDABLE RAIL (`rail_expand_enabled`; owner, 2026-11-21): off, the
// rail of before — no button, no titles; on and collapsed, the button and
// nothing more; expanded, every seat a row with its title, and the document
// told the rail's width.
describe("ClaudeRail: the expandable rail", () => {
  const BOT = { id: "b1", status: "idle", seq: 1, pinned: true, name: "Helper" };
  const titles = (r: ReactTestRenderer): string[] =>
    r.root.findAll((n) => n.props?.className === "claude-rail-title").map((n) => n.children.join(""));
  const titleIn = (n: ReactTestInstance): string | null => {
    const t = n.findAll((c) => c.props?.className === "claude-rail-title");
    return t.length === 1 ? t[0]!.children.join("") : null;
  };
  const expanded = (r: ReactTestRenderer) => String(aside(r).props.className).includes("is-expanded");
  // The document's root, which the test renderer's document lacks: what the
  // rail says to it (`railUp`).
  const doc = (globalThis as unknown as { document: Record<string, unknown> }).document;
  let said: Map<string, string>;
  let hadRoot = false;
  let wasRoot: unknown;
  beforeEach(() => {
    _setUiLayoutForTest({ v: 1 });
    setRailState({ width: RAIL_DEFAULT_WIDTH, expanded: false }, false);
    resetBotsPulseForTests();
    said = new Map();
    hadRoot = "documentElement" in doc;
    wasRoot = doc.documentElement;
    doc.documentElement = {
      style: {
        setProperty: (k: string, v: string) => said.set(k, v),
        removeProperty: (k: string) => said.delete(k),
      },
      classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    };
  });
  afterEach(() => {
    resetBotsPulseForTests();
    if (hadRoot) doc.documentElement = wasRoot;
    else delete doc.documentElement;
  });

  test("flag off: no button, no titles, no rows — even with the layout saying expanded", async () => {
    setRailState({ width: 260, expanded: true }, false);
    const r = await mountRail({ paneOpen: false, chats: [chat("a")] });
    expect(byLabel(r, "Expand rail").length + byLabel(r, "Collapse rail").length).toBe(0);
    expect(titles(r)).toEqual([]);
    expect(expanded(r)).toBe(false);
    expect(aside(r).props.style).toBeUndefined();
    expect(said.get("--claude-rail-w")).toBe("48px");
  });

  test("flag on, collapsed: the button says Expand rail, above the nav doors; no titles, no rows", async () => {
    const r = await mountRail({ paneOpen: false, chats: [chat("a")], expand: true });
    const [btn] = byLabel(r, "Expand rail");
    expect(btn).toBeDefined();
    expect(btn!.props["aria-expanded"]).toBe(false);
    expect(btn!.props["data-hint"]).toBe("Expand rail");
    expect(btn!.props["data-hint-side"]).toBe("left");
    expect(btn!.props.title).toBeUndefined();
    expect(navOrder(r)).toEqual(["Bots", "Tasks", "New task"]);
    expect(titles(r)).toEqual([]);
    expect(expanded(r)).toBe(false);
    expect(said.get("--claude-rail-w")).toBe("48px");
    // The seam is still the pull that opens the pane.
    expect(seam(r)).toBe(1);
  });

  test("flag on, beside the open pane: the button stands there too", async () => {
    const open = await mountRail({ paneOpen: true, chats: [chat("a")], expand: true });
    expect(byLabel(open, "Expand rail").length).toBe(1);
  });

  test("flag on, expanded: rows with titles, the button says Collapse rail, the width said to the document", async () => {
    setRailState({ width: 260, expanded: true }, false);
    const r = await mountRail({ paneOpen: false, chats: [chat("a")], expand: true, botHost: false });
    await act(async () => {
      publishBots([BOT]);
    });
    expect(expanded(r)).toBe(true);
    expect(aside(r).props.style).toEqual({ flexBasis: 260, width: 260 });
    const [btn] = byLabel(r, "Collapse rail");
    expect(btn!.props["aria-expanded"]).toBe(true);
    expect(titleIn(byLabel(r, "Tasks")[0]!)).toBe("Tasks");
    expect(titleIn(byLabel(r, "New task")[0]!)).toBe("New task");
    const [ring] = r.root.findAll((n) => n.type === "a" && n.props["data-task-key"] === "a");
    expect(titleIn(ring!)).toBe("a");
    const bot = r.root.find((n) => n.type === "a" && n.props["data-bot-id"] === "b1");
    expect(titleIn(bot)).toBe("Helper");
    // The accessible names are untouched: the titles are for the eye.
    for (const t of r.root.findAll((n) => n.props?.className === "claude-rail-title")) expect(t.props["aria-hidden"]).toBe("true");
    expect(byLabel(r, "Tasks").length).toBe(1);
    // The dots and the pin mark ride the icon's own box, not the row.
    const icon = byLabel(r, "Tasks")[0]!.find((n) => n.props?.className === "claude-rail-icon");
    expect(icon).toBeDefined();
    expect(said.get("--claude-rail-w")).toBe("260px");
    // Expanded, the seam is the rail's resize, not the pull.
    expect(seam(r)).toBe(0);
    expect(byLabel(r, "Drag to resize the rail").length).toBe(1);
  });

  test("a press on the button toggles the store, and the document follows", async () => {
    const r = await mountRail({ paneOpen: false, chats: [chat("a")], expand: true });
    await act(async () => {
      byLabel(r, "Expand rail")[0]!.props.onClick();
    });
    expect(getRailState().expanded).toBe(true);
    expect(expanded(r)).toBe(true);
    expect(said.get("--claude-rail-w")).toBe(`${RAIL_DEFAULT_WIDTH}px`);
    await act(async () => {
      byLabel(r, "Collapse rail")[0]!.props.onClick();
    });
    expect(getRailState().expanded).toBe(false);
    expect(said.get("--claude-rail-w")).toBe("48px");
    expect(titles(r)).toEqual([]);
  });
});
