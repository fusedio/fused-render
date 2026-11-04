// The shell's favourite card and the page's rails (fav-peek-store): a stack
// of rails, the last one still mounted is the one the card stands beside;
// a keyboard press's wish to focus the card waits for the card's key.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, describe, expect, it } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { TaskPulseTask } from "@platform/lib/api";
import { favOpen, setFavOpen } from "@platform/lib/last-chat";
import {
  PENDING_FOCUS_MS,
  favPeekState,
  focusRail,
  railHere,
  registerRail,
  requestFavFocus,
  resetFavPeekStoreForTests,
  takeFavFocus,
  takeQuietFocus,
  useFavOpenRow,
  withdrawFavFocusOn,
} from "./fav-peek-store";

const rail = (name: string) => ({ name, isConnected: true }) as unknown as HTMLElement;
const settle = () => new Promise<void>((res) => queueMicrotask(res));

afterEach(() => resetFavPeekStoreForTests());

describe("the rail stack", () => {
  it("the last registered rail is the card's", async () => {
    const a = rail("a");
    const b = rail("b");
    registerRail(a);
    registerRail(b);
    await settle();
    expect(favPeekState().rail).toBe(b);
  });

  it("a page hop's late cleanup — the OLD rail leaving after the new one came — keeps the new one", async () => {
    const a = rail("a");
    const b = rail("b");
    const offA = registerRail(a);
    registerRail(b);
    offA();
    await settle();
    expect(favPeekState().rail).toBe(b);
  });

  it("the top rail leaving hands the card back to the one under it, and the last one to none", async () => {
    const a = rail("a");
    const b = rail("b");
    const offA = registerRail(a);
    const offB = registerRail(b);
    offB();
    await settle();
    expect(favPeekState().rail).toBe(a);
    offA();
    await settle();
    expect(favPeekState().rail).toBeNull();
  });

  it("skips a rail that is off the page without having said so", async () => {
    const a = rail("a");
    const b = { isConnected: false } as unknown as HTMLElement;
    registerRail(a);
    registerRail(b);
    await settle();
    expect(favPeekState().rail).toBe(a);
  });
});

describe("the pending focus", () => {
  it("is taken once, by the card standing on that key", async () => {
    requestFavFocus("k1");
    await settle();
    expect(favPeekState().pendingFocus).toBe("k1");
    expect(takeFavFocus("k2")).toBe(false);
    expect(takeFavFocus("k1")).toBe(true);
    await settle();
    expect(favPeekState().pendingFocus).toBeNull();
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("a withdrawn wish (the open write refused) is not taken", () => {
    requestFavFocus("k1");
    requestFavFocus(null);
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("a stale wish is spent without taking the keyboard", () => {
    const realNow = Date.now;
    try {
      requestFavFocus("k1");
      const t0 = realNow();
      Date.now = () => t0 + PENDING_FOCUS_MS + 1;
      expect(takeFavFocus("k1")).toBe(false);
      expect(takeFavFocus("k1")).toBe(false);
    } finally {
      Date.now = realNow;
    }
  });
});

describe("the pending focus, withdrawn when the reader moves on", () => {
  // A focus target as `withdrawFavFocusOn` reads it: its tag and the ring
  // (`data-task-key`) it sits in, if any.
  const target = (tagName: string, ringKey: string | null = null) =>
    ({
      tagName,
      closest: () => (ringKey === null ? null : { dataset: { taskKey: ringKey } }),
    }) as unknown as Element;
  const card = (...inside: Element[]) => ({ contains: (n: Node) => inside.includes(n as unknown as Element) }) as Element;

  it("focus on another control withdraws it", () => {
    requestFavFocus("k1");
    withdrawFavFocusOn(target("INPUT"), card());
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("another ring withdraws it; the ring that asked does not", () => {
    requestFavFocus("k1");
    withdrawFavFocusOn(target("BUTTON", "k1"), card());
    expect(takeFavFocus("k1")).toBe(true);
    requestFavFocus("k1");
    withdrawFavFocusOn(target("BUTTON", "k2"), card());
    expect(takeFavFocus("k1")).toBe(false);
  });

  it("focus inside the card, on the body (a removed ring), or on no element keeps it", () => {
    const x = target("BUTTON");
    requestFavFocus("k1");
    withdrawFavFocusOn(x, card(x));
    withdrawFavFocusOn(target("BODY"), card());
    withdrawFavFocusOn({} as EventTarget, card());
    withdrawFavFocusOn(null, card());
    expect(takeFavFocus("k1")).toBe(true);
  });

  it("the rail itself (where focusRail parks the keyboard) keeps it", () => {
    const r = Object.assign(target("NAV"), { isConnected: true }) as unknown as HTMLElement;
    registerRail(r);
    requestFavFocus("k1");
    withdrawFavFocusOn(r, card());
    expect(takeFavFocus("k1")).toBe(true);
  });

  it("with no wish pending, nothing happens", async () => {
    withdrawFavFocusOn(target("INPUT"), null);
    await settle();
    expect(favPeekState().pendingFocus).toBeNull();
  });
});

describe("focusRail", () => {
  // A rail as focusRail reads it: rings by section, each with its key.
  type Ring = { section: "favs" | "chats"; key: string };
  const fakeRail = (rings: Ring[]) => {
    const focused: string[] = [];
    const els = rings.map((r) => ({
      dataset: { taskKey: r.key },
      closest: (sel: string) => (sel === `.claude-rail-${r.section}` ? {} : null),
      focus: () => focused.push(`${r.section}:${r.key}`),
      section: r.section,
    }));
    const el = {
      isConnected: true,
      querySelectorAll: (sel: string) => {
        const m = /^\.claude-rail-(favs|chats) /.exec(sel);
        return m ? els.filter((e) => e.section === m[1]) : els;
      },
      hasAttribute: () => true,
      setAttribute: () => {},
      focus: () => focused.push("rail"),
    } as unknown as HTMLElement;
    return { el, focused };
  };
  it("a chat that just became a favourite: its ring in the favourites section, not the list's first", () => {
    const { el, focused } = fakeRail([
      { section: "chats", key: "x" },
      { section: "favs", key: "k" },
    ]);
    focusRail(el, "k", "foot");
    expect(focused).toEqual(["favs:k"]);
  });
  it("the pulse has not moved the ring yet: the list's ring for the key", () => {
    const { el, focused } = fakeRail([
      { section: "chats", key: "x" },
      { section: "chats", key: "k" },
    ]);
    focusRail(el, "k", "foot");
    expect(focused).toEqual(["chats:k"]);
  });
  it("the list first when asked, and an unstarred foot ring is skipped for the first other ring", () => {
    const a = fakeRail([
      { section: "favs", key: "k" },
      { section: "chats", key: "k" },
    ]);
    focusRail(a.el, "k", "list");
    expect(a.focused).toEqual(["chats:k"]);
    const b = fakeRail([
      { section: "favs", key: "k" },
      { section: "chats", key: "y" },
    ]);
    focusRail(b.el, "k", "list", true);
    // Never another chat's ring (owner, 2026-11-04: "a random task from
    // above opens"): the unstarred chat has no list ring here — the rail.
    expect(b.focused).toEqual(["rail"]);
  });
  it("a ring it focuses is marked quiet for that one focus (no hover card); the rail is not", () => {
    const seen: boolean[] = [];
    const { el } = fakeRail([{ section: "chats", key: "k" }]);
    const ring = (el.querySelectorAll(".claude-rail-chat") as unknown as { focus: () => void }[])[0]!;
    const plain = ring.focus;
    ring.focus = () => {
      plain();
      seen.push(takeQuietFocus(ring));
    };
    focusRail(el, "k", "list", true);
    expect(seen).toEqual([true]);
    // Spent: a later, real focus is heard.
    expect(takeQuietFocus(ring)).toBe(false);
  });
  it("no ring at all: the rail itself", () => {
    const { el, focused } = fakeRail([]);
    focusRail(el, "k", "foot");
    expect(focused).toEqual(["rail"]);
  });
});

describe("the page's canvas rides with its rail", () => {
  it("railHere answers the registered getter, live, and nothing once the rail leaves", () => {
    const a = rail("a");
    let file: string | null = null;
    const select = () => {};
    const off = registerRail(a, () => ({ env: "prod", name: "c", file, select }));
    expect(railHere(a)).toEqual({ env: "prod", name: "c", file: null, select });
    file = "hex.py";
    expect(railHere(a)?.file).toBe("hex.py");
    off();
    expect(railHere(a)).toBeNull();
    // A rail with no canvas (a folder's), and no rail.
    registerRail(rail("b"));
    expect(railHere(rail("b"))).toBeNull();
    expect(railHere(null)).toBeNull();
  });
});

describe("useFavOpenRow (the open favourite's verdict)", () => {
  const g = globalThis as { sessionStorage?: unknown };
  function withStorage(fn: () => void) {
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>();
    g.sessionStorage = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    try {
      fn();
    } finally {
      act(() => setFavOpen(null));
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  }
  const row = (over: Partial<TaskPulseTask>): TaskPulseTask =>
    ({ key: "F", status: "done", starred: true, unread: 0, session_id: "s-F", ...over }) as TaskPulseTask;
  function mount(rows: TaskPulseTask[]) {
    const seen: (string | null)[] = [];
    let current = rows;
    function Probe({ rows: r }: { rows: TaskPulseTask[] }) {
      seen.push(useFavOpenRow(true, r)?.key ?? null);
      return null;
    }
    let r!: ReactTestRenderer;
    act(() => {
      r = create(createElement(Probe, { rows: current }));
    });
    return {
      seen,
      pulse(next: TaskPulseTask[]) {
        current = next;
        act(() => r.update(createElement(Probe, { rows: current })));
      },
      unmount: () => act(() => r.unmount()),
    };
  }

  for (const [what, next] of [
    ["an unstar", [row({ starred: false })]],
    ["an erase (the row gone)", [row({ key: "other" })]],
    ["an archive", [row({ status: "archived" })]],
  ] as const) {
    it(`${what} in the pulse closes the card and clears this window's key`, () => {
      withStorage(() => {
        act(() => setFavOpen("F"));
        const m = mount([row({})]);
        expect(m.seen[m.seen.length - 1]).toBe("F");
        m.pulse([...next]);
        expect(m.seen[m.seen.length - 1]).toBeNull();
        expect(favOpen()).toBeNull();
        m.unmount();
      });
    });
  }

  it("a star pressed a moment ago (the pulse not caught up) shows and keeps the key", () => {
    withStorage(() => {
      act(() => setFavOpen("F"));
      const m = mount([row({ starred: false })]);
      expect(m.seen[m.seen.length - 1]).toBe("F");
      expect(favOpen()).toBe("F");
      m.unmount();
    });
  });
});

