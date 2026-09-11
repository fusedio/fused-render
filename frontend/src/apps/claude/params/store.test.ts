// The two param stores behind the native chat: the URL one must reproduce
// runtime.js's coalescing rule (overlay first, ≤1 history write per 400 ms,
// traversal drops the pending write, pagehide flushes, `_layout` preserved),
// the memory one the same contract with no URL at all.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createMemoryParamsStore, createUrlParamsStore, HISTORY_MIN_INTERVAL_MS, PARAM_ENTRY_FLAG } =
  await import("./store");

/** A fake browser: one URL, a real state slot, manual clock and timers. */
function fakeEnv(url: string) {
  let current = url;
  let now = 10_000;
  const timers: Array<{ fn: () => void; at: number }> = [];
  const writes: Array<{ kind: "replace" | "push"; url: string }> = [];
  const events = new CountingTarget();
  let state: unknown = null;
  let dispatched = 0;
  const env = {
    location: {
      get pathname() {
        return current.split("?")[0];
      },
      get search() {
        const q = current.indexOf("?");
        return q === -1 ? "" : current.slice(q);
      },
    },
    history: {
      get state() {
        return state;
      },
      pushState(s: unknown, _u: string, u: string) {
        state = s;
        current = u;
        writes.push({ kind: "push", url: u });
        events.dispatchEvent(new Event("fused:urlchange"));
      },
    },
    replace(u: string) {
      current = u;
      writes.push({ kind: "replace", url: u });
      events.dispatchEvent(new Event("fused:urlchange"));
    },
    events,
    dispatchUrlChange() {
      dispatched++;
      events.dispatchEvent(new Event("fused:urlchange"));
    },
    now: () => now,
    setTimeout(fn: () => void, ms: number) {
      const t = { fn, at: now + ms };
      timers.push(t);
      return t;
    },
    clearTimeout(id: unknown) {
      const i = timers.indexOf(id as { fn: () => void; at: number });
      if (i >= 0) timers.splice(i, 1);
    },
  };
  const tick = (ms: number) => {
    now += ms;
    for (const t of timers.splice(0).filter((t) => t.at <= now)) t.fn();
  };
  const navigate = (u: string) => {
    current = u;
  };
  /** R:1056 — the store only spends the visit's push on a write the user
   *  caused, and a capture-phase pointerdown is how it knows. */
  const gesture = () => events.dispatchEvent(new Event("pointerdown"));
  return {
    env,
    writes,
    tick,
    navigate,
    events,
    gesture,
    dispatched: () => dispatched,
    url: () => current,
  };
}

/** A COUNTING event target: the store's five window listeners bind on demand
 *  (a subscriber, or an explicit `attach()`), and "on demand" is only worth
 *  anything if a store nobody wants really is holding none. */
class CountingTarget extends EventTarget {
  live = 0;
  override addEventListener(
    type: string,
    cb: EventListenerOrEventListenerObject | null,
    opts?: boolean | AddEventListenerOptions,
  ) {
    this.live += 1;
    super.addEventListener(type, cb, opts);
  }
  override removeEventListener(
    type: string,
    cb: EventListenerOrEventListenerObject | null,
    opts?: boolean | EventListenerOptions,
  ) {
    this.live -= 1;
    super.removeEventListener(type, cb, opts);
  }
}

/** The store as a MOUNT holds it: `attach()`ed, exactly what `ClaudeChat`'s
 *  effect does. The constructor deliberately binds nothing (store.ts `sync`),
 *  so every test that dispatches a window event — a gesture included — needs
 *  the store attached first, just like the app. */
function mountedStore(env: ReturnType<typeof fakeEnv>["env"]) {
  const store = createUrlParamsStore(env);
  store.attach();
  return store;
}

describe("createUrlParamsStore", () => {
  test("a write is visible at once and lands in history immediately when the budget allows", () => {
    const f = fakeEnv("/explorer/view/a?_side=claude");
    const store = mountedStore(f.env);
    store.set({ session_id: "s1" });
    expect(store.get("session_id")).toBe("s1");
    expect(f.writes).toEqual([{ kind: "replace", url: "/explorer/view/a?_side=claude&session_id=s1" }]);
  });

  test("a burst coalesces to one trailing write with the final value", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    store.set({ split: "50" }); // spends the budget
    store.set({ split: "51" });
    store.set({ split: "52" });
    expect(store.get("split")).toBe("52"); // overlay serves readers
    expect(f.writes.length).toBe(1);
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.writes.length).toBe(2);
    expect(f.url()).toBe("/x?a=1&split=52");
  });

  test("null removes the key; a no-op write does not touch history", () => {
    const f = fakeEnv("/x?run=r1&a=1");
    const store = mountedStore(f.env);
    store.set({ run: null });
    expect(store.get("run")).toBeUndefined();
    expect(f.url()).toBe("/x?a=1");
    const n = f.writes.length;
    store.set({ a: "1" });
    expect(f.writes.length).toBe(n);
  });

  test("a traversal drops the pending write and notifies with settled state", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    store.set({ a: "2" }); // immediate
    store.set({ a: "3" }); // queued
    const seen: string[] = [];
    store.onChange((all) => seen.push(all.a ?? "<none>"));
    f.navigate("/x?a=1"); // Back landed on the old entry
    f.events.dispatchEvent(new Event("popstate"));
    expect(seen).toEqual(["1"]);
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.url()).toBe("/x?a=1"); // nothing landed late
  });

  test("a pending write aimed at a page we left is dropped, not invented on the new one", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    store.set({ a: "2" });
    store.set({ zz: "8" });
    f.navigate("/y");
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.url()).toBe("/y");
    expect(store.get("zz")).toBeUndefined();
  });

  test("pagehide flushes the pending write", () => {
    const f = fakeEnv("/x");
    const store = mountedStore(f.env);
    store.set({ a: "1" });
    store.set({ a: "2" });
    f.events.dispatchEvent(new Event("pagehide"));
    expect(f.url()).toBe("/x?a=2");
  });

  test("a bare set spends the visit's one push after a gesture, later ones replace", () => {
    const f = fakeEnv("/x");
    const store = mountedStore(f.env);
    f.gesture();
    store.set({ a: "1" });
    expect(f.writes[0]?.kind).toBe("push");
    expect((f.env.history.state as Record<string, unknown>)[PARAM_ENTRY_FLAG]).toBe(true);
    store.set({ a: "2" });
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.writes.filter((w) => w.kind === "push").length).toBe(1);
    expect(f.url()).toBe("/x?a=2");
  });

  test("a write before any gesture coalesces and leaves the entry pristine (R:1056)", () => {
    // The seeding view's Back trap: a param the page computes for itself at boot
    // describes the state it already loaded in, so it must not cost an entry —
    // and the user's first real change must still get one.
    const f = fakeEnv("/x");
    const store = mountedStore(f.env);
    store.set({ session_id: "s1" });
    expect(f.writes.map((w) => w.kind)).toEqual(["replace"]);
    expect(f.env.history.state).toBe(null);
    f.gesture();
    store.set({ permission: "auto" });
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.writes.some((w) => w.kind === "push")).toBe(true);
  });

  test("history:replace never pushes, gesture or not", () => {
    const f = fakeEnv("/x");
    const store = mountedStore(f.env);
    f.gesture();
    store.set({ run: "r1" }, { history: "replace" });
    f.tick(HISTORY_MIN_INTERVAL_MS);
    expect(f.writes.every((w) => w.kind === "replace")).toBe(true);
    expect(f.env.history.state).toBe(null); // and the entry stays pristine
  });

  test("A SPENT ANCHOR MUST BE REMOVED WITH `replace` (T:12978-12984, P4-20)", () => {
    // The call site's reason, pinned here because the store is where the two
    // roads differ. T: "arriving on the message is not a place anyone navigated
    // to twice, and a Back that re-fired the flare would be a history entry
    // nobody made. Left behind it would also re-scroll a reload the reader has
    // since scrolled away from."
    //
    // A bare `set` reaches the replace path only while no gesture has happened
    // — and `msg` is spent when the turn it names is ON SCREEN, which is
    // normally after the reader has clicked something.
    const bare = fakeEnv("/x?msg=u1");
    const bareStore = mountedStore(bare.env);
    bare.gesture();
    bareStore.set({ msg: null });
    expect(bare.writes.some((w) => w.kind === "push")).toBe(true);

    const asked = fakeEnv("/x?msg=u1");
    const askedStore = mountedStore(asked.env);
    asked.gesture();
    askedStore.set({ msg: null }, { history: "replace" });
    asked.tick(HISTORY_MIN_INTERVAL_MS);
    expect(asked.writes.every((w) => w.kind === "replace")).toBe(true);
    expect(asked.url()).toBe("/x");
  });

  test("the _layout span is preserved raw and last (D51)", () => {
    const f = fakeEnv("/x?_layout=(row(a)(b))&a=1");
    const store = mountedStore(f.env);
    expect(store.getAll()).toEqual({ a: "1" });
    store.set({ a: "2" });
    expect(f.url()).toBe("/x?a=2&_layout=(row(a)(b))");
  });

  test("a _layout span carrying separators is NOT re-encoded (R:989)", () => {
    // `urlSafeLayout` escapes `%`, `#` and space and nothing else; a `,` or `=`
    // inside the span has to survive a chat param write byte-for-byte or it
    // stops matching what the layout writer produces.
    const raw = "row(a=1,b/c)(d)";
    const f = fakeEnv(`/x?_layout=(${raw})&a=1`);
    const store = mountedStore(f.env);
    store.set({ a: "2" });
    expect(f.url()).toBe(`/x?a=2&_layout=(${raw})`);
  });

  test("a reserved `_` key is refused, not written (R:885)", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    store.set({ _file: "/w/app", a: "2" });
    expect(store.get("_file")).toBeUndefined();
    expect(f.url()).toBe("/x?a=2");
  });

  test("every set announces on the event path, queued or landed (R:1294)", () => {
    const f = fakeEnv("/x");
    const store = mountedStore(f.env);
    store.set({ paneview: "chat" }); // lands at once
    store.set({ paneview: "preview" }); // queued behind the 400 ms budget
    expect(f.dispatched()).toBe(2);
  });

  test("a reordering rewrite is not a change (key-order-insensitive)", () => {
    const f = fakeEnv("/x?a=1&b=2");
    const store = mountedStore(f.env);
    let n = 0;
    store.onChange(() => n++);
    f.navigate("/x?b=2&a=1");
    f.events.dispatchEvent(new Event("fused:urlchange"));
    expect(n).toBe(0);
  });

  test("a store nobody has attached or subscribed to binds NOTHING", () => {
    // The leak this is about: a `useState` initializer React discards
    // (StrictMode double-invokes it) used to leave five capture-phase window
    // listeners behind with nothing able to detach them.
    const f = fakeEnv("/x?a=1");
    const store = createUrlParamsStore(f.env);
    expect(f.events.live).toBe(0);
    // …and it is still a working reader, just a deaf one.
    expect(store.get("a")).toBe("1");
  });

  test("the first subscriber binds and the last one to leave unbinds", () => {
    const f = fakeEnv("/x?a=1");
    const store = createUrlParamsStore(f.env);
    const off1 = store.onChange(() => {});
    expect(f.events.live).toBe(5);
    const off2 = store.onChange(() => {});
    expect(f.events.live).toBe(5); // one binding, not one per subscriber
    off1();
    expect(f.events.live).toBe(5);
    off2();
    expect(f.events.live).toBe(0);
  });

  test("attach holds the binding open with no subscriber, and dispose gives it up", () => {
    // A mount attaches before anything subscribes on purpose: `pointerdown` /
    // `keydown` have to be armed before the user's first gesture, which is what
    // unlocks the visit's one history push.
    const f = fakeEnv("/x?a=1");
    const store = createUrlParamsStore(f.env);
    store.attach();
    expect(f.events.live).toBe(5);
    store.attach(); // idempotent
    expect(f.events.live).toBe(5);
    const off = store.onChange(() => {});
    off();
    expect(f.events.live).toBe(5); // the owner still wants it
    store.dispose();
    expect(f.events.live).toBe(0);
  });

  test("dispose drops the listeners and the subscribers", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    let n = 0;
    store.onChange(() => n++);
    store.dispose();
    f.navigate("/x?a=9");
    f.events.dispatchEvent(new Event("fused:urlchange"));
    expect(n).toBe(0);
    // …and re-attaches, so a StrictMode remount is not deaf (MINOR: :212).
    store.attach();
    f.navigate("/x?a=7");
    f.events.dispatchEvent(new Event("fused:urlchange"));
    expect(n).toBe(0); // the subscriber list was cleared with the listeners
    store.onChange(() => n++);
    f.navigate("/x?a=8");
    f.events.dispatchEvent(new Event("fused:urlchange"));
    expect(n).toBe(1);
  });

  test("onChange fires once per real change, including outside URL writes", () => {
    const f = fakeEnv("/x?a=1");
    const store = mountedStore(f.env);
    let n = 0;
    store.onChange(() => n++);
    f.navigate("/x?a=1");
    f.events.dispatchEvent(new Event("fused:urlchange")); // same snapshot
    expect(n).toBe(0);
    f.navigate("/x?a=5");
    f.events.dispatchEvent(new Event("fused:urlchange"));
    expect(n).toBe(1);
    expect(store.get("a")).toBe("5");
  });
});

describe("createMemoryParamsStore", () => {
  test("seeds, sets, removes and notifies without any URL", () => {
    const store = createMemoryParamsStore({ session_id: "s", run: "r" });
    const seen: Array<Record<string, string>> = [];
    store.onChange((all) => seen.push(all));
    store.set({ run: null, split: "40" });
    expect(store.getAll()).toEqual({ session_id: "s", split: "40" });
    store.set({ split: "40" }); // no-op
    expect(seen.length).toBe(1);
    const off = store.onChange(() => seen.push({}));
    off();
    store.set({ split: "41" });
    expect(seen.length).toBe(2);
  });
});
