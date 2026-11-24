// Which chats are on screen elsewhere (peek-session): a pane chat says what it
// shows while mounted and takes it back on unmount — the favourite's card
// reads only that, never the URL.
import { describe, expect, it } from "bun:test";
import { announcePaneSession, onPaneSessionsStorage, paneSessions } from "./peek-session";

describe("announcePaneSession", () => {
  it("lists a session while a mount holds it, deduplicated, and drops it with the last mount", () => {
    expect(paneSessions()).toEqual([]);
    const a = announcePaneSession("s1");
    const b = announcePaneSession("s1");
    const c = announcePaneSession("s0");
    expect(paneSessions()).toEqual(["s0", "s1"]);
    const snap = paneSessions();
    a();
    // One mount still on s1: nothing changed, the same snapshot.
    expect(paneSessions()).toBe(snap);
    b();
    c();
    expect(paneSessions()).toEqual([]);
  });

  // THE NODE CHAT'S FRAME (CanvasWorkspace `preview-side-frame`) is another
  // document with its own module copy: its announcements reach the shell
  // through sessionStorage and the `storage` event.
  function withStorage(run: (store: Map<string, string>, area: Storage) => void, refuse = false): void {
    const g = globalThis as { sessionStorage?: unknown };
    const had = Object.prototype.hasOwnProperty.call(g, "sessionStorage");
    const prev = g.sessionStorage;
    const store = new Map<string, string>();
    const fake = {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => {
        if (refuse) throw new Error("QuotaExceededError");
        store.set(k, v);
      },
      removeItem: (k: string) => {
        if (refuse) throw new Error("QuotaExceededError");
        store.delete(k);
      },
    };
    g.sessionStorage = fake;
    try {
      run(store, fake as unknown as Storage);
    } finally {
      // Put back exactly what was there (never delete a global another
      // suite installed).
      if (had) g.sessionStorage = prev;
      else delete g.sessionStorage;
    }
  }
  const KEY = "fused-render:pane-sessions";

  it("lists another document's pane chat on its storage event, and drops it when that document takes it back", () => {
    withStorage((store, area) => {
      store.set(KEY, JSON.stringify([{ sid: "node-1", doc: "frame-a" }]));
      // Another storage area, or another key: not ours.
      onPaneSessionsStorage({ key: KEY, storageArea: {} as Storage });
      onPaneSessionsStorage({ key: "other", storageArea: area });
      expect(paneSessions()).toEqual([]);
      onPaneSessionsStorage({ key: KEY, storageArea: area });
      expect(paneSessions()).toEqual(["node-1"]);
      // This document's own mount joins the union, and writes beside the
      // frame's entry without touching it.
      const off = announcePaneSession("pane-1");
      expect(paneSessions()).toEqual(["node-1", "pane-1"]);
      const stored = JSON.parse(store.get(KEY) ?? "[]") as { sid: string; doc: string }[];
      expect(stored.map((e) => e.sid).sort()).toEqual(["node-1", "pane-1"]);
      expect(stored.find((e) => e.sid === "node-1")?.doc).toBe("frame-a");
      off();
      expect(JSON.parse(store.get(KEY) ?? "[]")).toEqual([{ sid: "node-1", doc: "frame-a" }]);
      // The frame unmounts its chat (or goes with `pagehide`).
      store.delete(KEY);
      onPaneSessionsStorage({ key: KEY, storageArea: area });
      expect(paneSessions()).toEqual([]);
    });
  });

  it("garbage in storage reads as nothing", () => {
    withStorage((store, area) => {
      store.set(KEY, "{not json");
      onPaneSessionsStorage({ key: KEY, storageArea: area });
      expect(paneSessions()).toEqual([]);
      store.set(KEY, JSON.stringify([{ sid: 3 }, null, { sid: "ok", doc: "d" }]));
      onPaneSessionsStorage({ key: null, storageArea: area });
      expect(paneSessions()).toEqual(["ok"]);
      store.delete(KEY);
      onPaneSessionsStorage({ key: null, storageArea: area });
    });
  });

  it("a storage that refuses writes still leaves this document's own mounts listed", () => {
    withStorage((store) => {
      const off = announcePaneSession("solo");
      expect(paneSessions()).toEqual(["solo"]);
      expect(store.has(KEY)).toBe(false);
      off();
      expect(paneSessions()).toEqual([]);
    }, true);
  });
});
