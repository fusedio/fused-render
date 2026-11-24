import { describe, expect, it } from "bun:test";
import {
  aPinned,
  aPins,
  addAPin,
  addBotPin,
  addFavPin,
  adoptLegacyWindowBoxes,
  botOpen,
  botPinId,
  botPins,
  dropAPin,
  dropBotPin,
  dropFavPin,
  favPinId,
  favPinned,
  favPins,
  hasAPin,
  isPinnedId,
  parsePinList,
  pinIdSection,
  pinPosIds,
  pinPosRaw,
  pinnedIds,
  serialisePinList,
  setAPinned,
  setBotOpen,
  setFavPinned,
  setPinPosRaw,
  setWindowPosMark,
  clearFavOpen,
  favOpen,
  favOpenedAtMs,
  onFavOpenStorage,
  setFavOpen,
  subscribeFavOpen,
  lastChat,
  lastChatId,
  lastFav,
  onLastChatStorage,
  onLastFavStorage,
  setLastChat,
  setLastFav,
  subscribeLastChat,
  subscribeLastFav,
} from "./last-chat";

describe("last-chat", () => {
  it("names a row by project and task number, not by key", () => {
    const queued = { project: "/p", task_id: "TASK-3", key: "pending:e1" };
    const ran = { project: "/p", task_id: "TASK-3", key: "sess-1" };
    expect(lastChatId(queued)).toBe(lastChatId(ran));
    // The number is per project: the same number elsewhere is another chat.
    expect(lastChatId({ ...ran, project: "/q" })).not.toBe(lastChatId(ran));
    // No number yet: the key stands in.
    expect(lastChatId({ project: "/p", task_id: "", key: "new:/p/a.py" })).toContain("new:/p/a.py");
  });

  it("a press moves the mark and tells every reader", () => {
    let calls = 0;
    const off = subscribeLastChat(() => calls++);
    setLastChat("a");
    expect(lastChat()).toBe("a");
    setLastChat("b");
    expect(lastChat()).toBe("b");
    expect(calls).toBe(2);
    // The same pick again is no change.
    setLastChat("b");
    expect(calls).toBe(2);
    off();
    setLastChat("c");
    expect(calls).toBe(2);
  });

  it("a pick storage refused survives a new reader subscribing", () => {
    // No sessionStorage in this runtime: the write throws, so `mem` is the
    // only copy, and a subscribe must not re-read it away.
    setLastChat("kept");
    const off = subscribeLastChat(() => {});
    expect(lastChat()).toBe("kept");
    off();
  });

  it("another document's write is adopted and announced", () => {
    const g = globalThis as { sessionStorage?: unknown };
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>([["fused-render:last-chat", "remote"]]);
    const fake = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    g.sessionStorage = fake;
    try {
      let calls = 0;
      const off = subscribeLastChat(() => calls++);
      setLastChat("local");
      calls = 0;
      store.set("fused-render:last-chat", "remote");
      // Another storage area, or another key: not ours.
      onLastChatStorage({ key: "fused-render:last-chat", storageArea: {} as Storage });
      onLastChatStorage({ key: "other", storageArea: fake as unknown as Storage });
      expect(lastChat()).toBe("local");
      expect(calls).toBe(0);
      onLastChatStorage({ key: "fused-render:last-chat", storageArea: fake as unknown as Storage });
      expect(lastChat()).toBe("remote");
      expect(calls).toBe(1);
      off();
    } finally {
      // Put back exactly what was there (never delete a global another
      // suite installed).
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  });

  it("the favourites keep their own mark: a press in one never moves the other", () => {
    let chats = 0;
    let favs = 0;
    const offC = subscribeLastChat(() => chats++);
    const offF = subscribeLastFav(() => favs++);
    setLastChat("list-1");
    setLastFav("fav-1");
    expect(lastChat()).toBe("list-1");
    expect(lastFav()).toBe("fav-1");
    setLastChat("list-2");
    expect(lastFav()).toBe("fav-1");
    setLastFav("fav-2");
    expect(lastChat()).toBe("list-2");
    // Each store tells only its own readers.
    expect(chats).toBe(2);
    expect(favs).toBe(2);
    offC();
    offF();
  });

  it("each mark syncs on its own storage key", () => {
    const g = globalThis as { sessionStorage?: unknown };
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>();
    const fake = { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => void store.set(k, v) };
    g.sessionStorage = fake;
    try {
      const offC = subscribeLastChat(() => {});
      const offF = subscribeLastFav(() => {});
      setLastChat("l");
      setLastFav("f");
      expect(store.get("fused-render:last-chat")).toBe("l");
      expect(store.get("fused-render:last-fav")).toBe("f");
      store.set("fused-render:last-fav", "f-remote");
      // The list's listener ignores the favourites' key, and back.
      onLastChatStorage({ key: "fused-render:last-fav", storageArea: fake as unknown as Storage });
      expect(lastChat()).toBe("l");
      onLastFavStorage({ key: "fused-render:last-fav", storageArea: fake as unknown as Storage });
      expect(lastFav()).toBe("f-remote");
      expect(lastChat()).toBe("l");
      offC();
      offF();
    } finally {
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  });

  it("the open favourite: a press sets it, the × clears it, per window, on its own key", () => {
    const g = globalThis as { sessionStorage?: unknown };
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>();
    const fake = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    g.sessionStorage = fake;
    try {
      let calls = 0;
      const off = subscribeFavOpen(() => calls++);
      setFavOpen("A");
      expect(favOpen()).toBe("A");
      expect(store.get("fused-render:fav-open")).toBe("A");
      expect(favOpenedAtMs("A")).not.toBeNull();
      expect(favOpenedAtMs("B")).toBeNull();
      // The list's last mark is untouched.
      expect(store.has("fused-render:last-chat")).toBe(false);
      // A verdict about another chat closes nothing.
      clearFavOpen("B");
      expect(favOpen()).toBe("A");
      clearFavOpen("A");
      expect(favOpen()).toBeNull();
      expect(store.has("fused-render:fav-open")).toBe(false);
      expect(favOpenedAtMs("A")).toBeNull();
      expect(calls).toBe(2);
      // The node chat's frame (same window) wrote it: adopted.
      store.set("fused-render:fav-open", "C");
      onFavOpenStorage({ key: "fused-render:last-fav", storageArea: fake as unknown as Storage });
      expect(favOpen()).toBeNull();
      onFavOpenStorage({ key: "fused-render:fav-open", storageArea: fake as unknown as Storage });
      expect(favOpen()).toBe("C");
      // …and a frame's clear too.
      store.delete("fused-render:fav-open");
      onFavOpenStorage({ key: "fused-render:fav-open", storageArea: fake as unknown as Storage });
      expect(favOpen()).toBeNull();
      off();
    } finally {
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  });

  it("the open time rides beside the key in storage: another document of the window reads it", () => {
    const g = globalThis as { sessionStorage?: unknown };
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>();
    const fake = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, v),
      removeItem: (k: string) => void store.delete(k),
    };
    g.sessionStorage = fake;
    try {
      const off = subscribeFavOpen(() => {});
      setFavOpen("A");
      const at = favOpenedAtMs("A");
      expect(at).not.toBeNull();
      expect(JSON.parse(store.get("fused-render:fav-open-at")!)).toEqual({ key: "A", at });
      // The node chat's frame opened "N" (its own module copy wrote both
      // keys): this document adopts the key AND its open time.
      store.set("fused-render:fav-open-at", JSON.stringify({ key: "N", at: 1234 }));
      store.set("fused-render:fav-open", "N");
      onFavOpenStorage({ key: "fused-render:fav-open", storageArea: fake as unknown as Storage });
      expect(favOpen()).toBe("N");
      expect(favOpenedAtMs("N")).toBe(1234);
      expect(favOpenedAtMs("A")).toBeNull();
      // A frame's clear takes the time with it.
      store.delete("fused-render:fav-open-at");
      store.delete("fused-render:fav-open");
      onFavOpenStorage({ key: "fused-render:fav-open", storageArea: fake as unknown as Storage });
      expect(favOpenedAtMs("N")).toBeNull();
      // A garbled value is no time at all.
      store.set("fused-render:fav-open-at", "{oops");
      expect(favOpenedAtMs("N")).toBeNull();
      off();
    } finally {
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  });
});

// MANY PINS (owner, 2026-11-24): the three pin marks are lists, each pin with
// its own box; what the earlier build wrote still reads.
describe("the pin marks", () => {
  const settle = () => new Promise((r) => setTimeout(r, 0));
  it("a list is stored as the bare word while it has one item, as JSON past that, and reads back either way", () => {
    expect(serialisePinList([])).toBeNull();
    expect(serialisePinList(["task:k1"])).toBe("task:k1");
    expect(serialisePinList(["task:k1", "git"])).toBe('["task:k1","git"]');
    expect(parsePinList(null)).toEqual([]);
    expect(parsePinList("")).toEqual([]);
    // The earlier build's one word: a one-item list.
    expect(parsePinList("task:k1")).toEqual(["task:k1"]);
    expect(parsePinList("mode:read")).toEqual(["mode:read"]);
    expect(parsePinList('["task:k1","git"]')).toEqual(["task:k1", "git"]);
    // Junk inside the array is dropped, a repeat kept once; a bracket that is not JSON is a word.
    expect(parsePinList('["a",3,"a","",null,"b"]')).toEqual(["a", "b"]);
    expect(parsePinList("[oops")).toEqual(["[oops"]);
    for (const l of [[], ["x"], ["x", "y", "z"], ["[odd"]]) expect(parsePinList(serialisePinList(l))).toEqual(l);
  });
  it("adds and drops one pin without touching the others; the scalar face is the newest, and its set replaces all", async () => {
    setAPinned(null);
    addAPin("task:k1");
    addAPin("git");
    addAPin("task:k1");
    expect(aPins()).toEqual(["task:k1", "git"]);
    // The same list while nothing changed (a hook's snapshot).
    expect(aPins()).toBe(aPins());
    expect(aPinned()).toBe("git");
    expect(hasAPin("task:k1")).toBe(true);
    dropAPin("task:k1");
    expect(aPins()).toEqual(["git"]);
    setAPinned("plus");
    expect(aPins()).toEqual(["plus"]);
    setAPinned(null);
    expect(aPins()).toEqual([]);
    addFavPin("f1");
    addFavPin("f2");
    expect(favPins()).toEqual(["f1", "f2"]);
    expect(favPinned()).toBe("f2");
    dropFavPin("f1");
    expect(favPins()).toEqual(["f2"]);
    setFavPinned(null);
    addBotPin("b1");
    addBotPin("b2");
    expect(botPins()).toEqual(["b1", "b2"]);
    expect(botOpen()).toBe("b2");
    dropBotPin("b2");
    expect(botPins()).toEqual(["b1"]);
    setBotOpen(null);
    await settle();
  });
  it("a pin's id says its section and whether it is pinned now", async () => {
    expect(favPinId("k")).toBe("fav:k");
    expect(botPinId("b")).toBe("bot:b");
    expect(pinIdSection("fav:k")).toBe("b");
    expect(pinIdSection("bot:b")).toBe("b");
    expect(pinIdSection("task:k")).toBe("a");
    expect(pinIdSection("mode:read")).toBe("a");
    addAPin("tasks");
    addFavPin("k");
    addBotPin("b");
    expect(pinnedIds()).toEqual(["tasks", "fav:k", "bot:b"]);
    expect(isPinnedId("fav:k")).toBe(true);
    expect(isPinnedId("bot:k")).toBe(false);
    setAPinned(null);
    setFavPinned(null);
    setBotOpen(null);
    await settle();
  });
  it("each pin's box is its own; a pin let go has its box cleared a moment later", async () => {
    addAPin("task:k1");
    addAPin("git");
    setPinPosRaw("task:k1", '{"left":1,"top":2,"width":400,"height":500}');
    setPinPosRaw("git", '{"left":9,"top":9,"width":400,"height":500}');
    expect(pinPosIds().sort()).toEqual(["git", "task:k1"]);
    dropAPin("task:k1");
    // Still there for the card fading out…
    expect(pinPosRaw("task:k1")).not.toBeNull();
    await settle();
    // …then gone; the other pin's box stays.
    expect(pinPosRaw("task:k1")).toBeNull();
    expect(pinPosRaw("git")).toBe('{"left":9,"top":9,"width":400,"height":500}');
    setAPinned(null);
    await settle();
    setPinPosRaw("git", null);
    expect(pinPosIds()).toEqual([]);
  });
  it("THE EARLIER BUILD'S WINDOW BOX is adopted by the section's one pin — never by one of many", async () => {
    const raw = '{"left":40,"top":50,"width":420,"height":640}';
    setAPinned("task:k1");
    setWindowPosMark("a", raw);
    setFavPinned("f1");
    setWindowPosMark("b", '{"left":5,"top":6}');
    adoptLegacyWindowBoxes();
    expect(pinPosRaw("task:k1")).toBe(raw);
    // A box with no size (a pin dragged before free pins) is adopted as it is.
    expect(pinPosRaw("fav:f1")).toBe('{"left":5,"top":6}');
    // A pin with a box of its own keeps it.
    setWindowPosMark("a", '{"left":0,"top":0,"width":420,"height":640}');
    adoptLegacyWindowBoxes();
    expect(pinPosRaw("task:k1")).toBe(raw);
    // Two pins in a section: neither takes the window's box.
    setPinPosRaw("task:k1", null);
    addAPin("git");
    adoptLegacyWindowBoxes();
    expect(pinPosRaw("task:k1")).toBeNull();
    expect(pinPosRaw("git")).toBeNull();
    setAPinned(null);
    setFavPinned(null);
    await settle();
    setPinPosRaw("fav:f1", null);
    setWindowPosMark("a", null);
    setWindowPosMark("b", null);
  });
});
