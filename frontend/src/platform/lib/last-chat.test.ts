import { describe, expect, it } from "bun:test";
import {
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
