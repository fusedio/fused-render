import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { createPanelParamsStore, type PanelParamsHost } from "./panel-params";

// The store reads `window` (the gesture's listeners, its timers) and the
// host's `history.state`: a bare EventTarget window and a state slot stand in.
const g = globalThis as unknown as { window?: unknown; history?: unknown };
let saved: { window: unknown; history: unknown };
let win: EventTarget & { setTimeout: typeof setTimeout; clearTimeout: typeof clearTimeout };

beforeEach(() => {
  saved = { window: g.window, history: g.history };
  win = Object.assign(new EventTarget(), { setTimeout, clearTimeout });
  g.window = win;
  g.history = { state: null };
});
afterEach(() => {
  g.window = saved.window;
  g.history = saved.history;
});

function makeHost(initial = "") {
  let q = initial;
  const writes: { query: string; how: string; state?: unknown }[] = [];
  const host: PanelParamsHost = {
    query: () => q,
    write: (query, how, state) => {
      q = query;
      writes.push({ query, how, state });
    },
  };
  return { host, writes, set: (next: string) => (q = next) };
}

describe("panel chat params over `_right`", () => {
  test("reads the view's query as its params", () => {
    const { host } = makeHost("session_id=s1&split=1");
    const store = createPanelParamsStore(host, new EventTarget());
    expect(store.get("session_id")).toBe("s1");
    expect(store.getAll()).toEqual({ session_id: "s1", split: "1" });
  });

  test("a write before any gesture is a replace, never a history entry", () => {
    const { host, writes } = makeHost("");
    const store = createPanelParamsStore(host, new EventTarget());
    store.attach();
    store.set({ session_id: "s2" });
    expect(writes).toEqual([{ query: "session_id=s2", how: "replace", state: undefined }]);
    store.dispose();
  });

  test("after a gesture in the WINDOW, the first write pushes, flagged for the store", () => {
    const { host, writes } = makeHost("");
    const store = createPanelParamsStore(host, new EventTarget());
    store.attach();
    win.dispatchEvent(new Event("pointerdown"));
    store.set({ session_id: "s3" });
    expect(writes.length).toBe(1);
    expect(writes[0].how).toBe("push");
    expect(writes[0].query).toBe("session_id=s3");
    expect((writes[0].state as Record<string, unknown>).fusedParamEntry).toBe(true);
    store.dispose();
  });

  test("the host's popstate on the private target re-reads the query", () => {
    const { host, set } = makeHost("session_id=a");
    const events = new EventTarget();
    const store = createPanelParamsStore(host, events);
    const seen: string[] = [];
    const off = store.onChange((all) => seen.push(all.session_id ?? ""));
    set("session_id=b");
    events.dispatchEvent(new Event("popstate"));
    expect(seen).toEqual(["b"]);
    off();
  });
});
