// The shared-chat param mirror: shared wins at start, mount fills gaps, both
// directions follow, echoes stop, `{history}` passes through, unsubscribe ends it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { describe, expect, test } from "bun:test";

const { createMemoryParamsStore } = await import("./store");
const { mirrorParams } = await import("./mirror");
type ParamsStore = import("./store").ParamsStore;
type SetOpts = import("./store").SetOpts;

const KEYS = ["session_id", "run", "permission"] as const;

function last<T>(xs: T[]): T | undefined {
  return xs[xs.length - 1];
}

/** A memory store that records every `set` it receives (after the mirror taps it). */
function recording(initial: Record<string, string> = {}) {
  const store = createMemoryParamsStore(initial);
  const calls: Array<{ patch: Record<string, string | null>; opts?: SetOpts }> =
    [];
  const inner = store.set;
  store.set = (patch, opts) => {
    calls.push({ patch, opts });
    inner(patch, opts);
  };
  return { store, calls };
}

function changes(store: ParamsStore) {
  let n = 0;
  store.onChange(() => n++);
  return () => n;
}

describe("mirrorParams start", () => {
  test("shared value wins over the mount's; mount fills gaps (not run)", () => {
    const mount = createMemoryParamsStore({
      session_id: "m",
      run: "r1",
      permission: "plan",
    });
    const shared = createMemoryParamsStore({ session_id: "s" });
    mirrorParams(mount, shared, KEYS);
    expect(mount.get("session_id")).toBe("s");
    expect(shared.get("permission")).toBe("plan");
    // no run seed by default: the mount keeps its own, shared stays without
    expect(shared.get("run")).toBeUndefined();
    expect(mount.get("run")).toBe("r1");
  });

  test("seedRun copies the mount's run into a shared store that has none", () => {
    const mount = createMemoryParamsStore({ session_id: "s", run: "r1" });
    const shared = createMemoryParamsStore({});
    mirrorParams(mount, shared, KEYS, { seedRun: true });
    expect(shared.get("run")).toBe("r1");
    expect(shared.get("session_id")).toBe("s");
  });

  test("seedRun never overrides a shared run", () => {
    const mount = createMemoryParamsStore({ run: "stale" });
    const shared = createMemoryParamsStore({ run: "live" });
    mirrorParams(mount, shared, KEYS, { seedRun: true });
    expect(shared.get("run")).toBe("live");
    expect(mount.get("run")).toBe("live");
  });

  test("start writes into the mount use replace", () => {
    const { store: mount, calls } = recording({});
    const shared = createMemoryParamsStore({ session_id: "s" });
    mirrorParams(mount, shared, KEYS);
    expect(calls).toEqual([
      { patch: { session_id: "s" }, opts: { history: "replace" } },
    ]);
  });

  test("keys outside the list are left alone", () => {
    const mount = createMemoryParamsStore({ model: "opus" });
    const shared = createMemoryParamsStore({ model: "haiku" });
    mirrorParams(mount, shared, KEYS);
    expect(mount.get("model")).toBe("opus");
    shared.set({ model: "sonnet" });
    expect(mount.get("model")).toBe("opus");
  });
});

describe("mirrorParams live", () => {
  test("shared → mount, with the originating history passed through", () => {
    const { store: mount, calls } = recording({});
    const shared = createMemoryParamsStore({});
    mirrorParams(mount, shared, KEYS);
    shared.set({ run: "r9" }, { history: "replace" });
    shared.set({ session_id: "s1" }, { history: "push" });
    expect(mount.get("run")).toBe("r9");
    expect(mount.get("session_id")).toBe("s1");
    expect(calls).toEqual([
      { patch: { run: "r9" }, opts: { history: "replace" } },
      { patch: { session_id: "s1" }, opts: { history: "push" } },
    ]);
  });

  test("a bare shared set reaches the mount as a bare set", () => {
    const { store: mount, calls } = recording({});
    const shared = createMemoryParamsStore({});
    mirrorParams(mount, shared, KEYS);
    shared.set({ run: "r1" });
    expect(last(calls)).toEqual({ patch: { run: "r1" }, opts: undefined });
  });

  test("a change with no set in flight reaches the other side as replace", () => {
    const mount = createMemoryParamsStore({});
    const { store: shared, calls } = recording({});
    mirrorParams(mount, shared, KEYS);
    // A change the mount store reports without a `set` (a popstate on the URL store).
    let fire: ((all: Record<string, string>) => void) | null = null;
    const fakeMount = {
      ...mount,
      onChange: (cb: (all: Record<string, string>) => void) => {
        fire = cb;
        return () => {};
      },
    };
    mirrorParams(fakeMount, shared, KEYS);
    fire!({ run: "r7" });
    expect(last(calls)).toEqual({
      patch: { run: "r7" },
      opts: { history: "replace" },
    });
  });

  test("mount → shared, including removals", () => {
    const mount = createMemoryParamsStore({});
    const shared = createMemoryParamsStore({ session_id: "s", run: "r" });
    mirrorParams(mount, shared, KEYS);
    mount.set({ permission: "plan" });
    expect(shared.get("permission")).toBe("plan");
    mount.set({ run: null });
    expect(shared.get("run")).toBeUndefined();
    expect(mount.get("run")).toBeUndefined();
  });

  test("session_id never flows mount → shared (a swap, not a fact)", () => {
    const mount = createMemoryParamsStore({});
    const shared = createMemoryParamsStore({ session_id: "s" });
    mirrorParams(mount, shared, KEYS);
    mount.set({ session_id: "other" });
    expect(shared.get("session_id")).toBe("s");
    mount.set({ session_id: null });
    expect(shared.get("session_id")).toBe("s");
  });

  test("run/permission relay up only while both name the same session", () => {
    const mount = createMemoryParamsStore({});
    const shared = createMemoryParamsStore({ session_id: "s", run: "r" });
    mirrorParams(mount, shared, KEYS);
    // swapped away: the new session's run must not land in this chat
    mount.set({ session_id: "other", run: "r-other" });
    expect(shared.get("run")).toBe("r");
    mount.set({ permission: "plan" });
    expect(shared.get("permission")).toBeUndefined();
    // back on the same session: relays again
    mount.set({ session_id: "s" });
    mount.set({ run: "r2" });
    expect(shared.get("run")).toBe("r2");
  });

  test("a shared store with no session yet takes the mount's run", () => {
    const mount = createMemoryParamsStore({});
    const shared = createMemoryParamsStore({});
    mirrorParams(mount, shared, KEYS);
    mount.set({ session_id: "s", run: "r" });
    expect(shared.get("run")).toBe("r");
    expect(shared.get("session_id")).toBeUndefined();
  });

  test("loop-safe: one change each side, no echo writes", () => {
    const { store: mount, calls: mountCalls } = recording({});
    const { store: shared, calls: sharedCalls } = recording({});
    mirrorParams(mount, shared, KEYS);
    const mountN = changes(mount);
    const sharedN = changes(shared);
    shared.set({ session_id: "a" });
    expect(mountN()).toBe(1);
    expect(sharedN()).toBe(1);
    expect(sharedCalls.length).toBe(1); // the original only — no echo back
    expect(mountCalls.length).toBe(1);
    mount.set({ run: "b" });
    expect(sharedCalls.length).toBe(2);
    expect(mountCalls.length).toBe(2);
    expect(shared.get("run")).toBe("b");
  });

  test("an unrelated mount write does not push stale mirrored keys", () => {
    const mount = createMemoryParamsStore({});
    const { store: shared, calls } = recording({ session_id: "s" });
    mirrorParams(mount, shared, KEYS);
    const before = calls.length;
    mount.set({ model: "opus" });
    expect(calls.length).toBe(before);
  });

  test("two mounts on one shared store stay in step", () => {
    const shared = createMemoryParamsStore({});
    const a = createMemoryParamsStore({});
    const b = createMemoryParamsStore({});
    const offA = mirrorParams(a, shared, KEYS);
    const offB = mirrorParams(b, shared, KEYS);
    a.set({ permission: "plan" });
    expect(shared.get("permission")).toBe("plan");
    expect(b.get("permission")).toBe("plan");
    offA(); // out of order: B's mirror must keep working
    shared.set({ run: "r" }, { history: "replace" });
    expect(b.get("run")).toBe("r");
    expect(a.get("run")).toBeUndefined();
    offB();
  });

  test("unsubscribe stops both directions and restores set", () => {
    const mount = createMemoryParamsStore({});
    const shared = createMemoryParamsStore({});
    const origMountSet = mount.set;
    const origSharedSet = shared.set;
    const off = mirrorParams(mount, shared, KEYS);
    off();
    expect(mount.set).toBe(origMountSet);
    expect(shared.set).toBe(origSharedSet);
    shared.set({ session_id: "s" });
    mount.set({ run: "r" });
    expect(mount.get("session_id")).toBeUndefined();
    expect(shared.get("run")).toBeUndefined();
    off(); // idempotent
  });
});
