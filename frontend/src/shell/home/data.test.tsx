// Home's two live widgets (data.ts): the bots list follows topic `bots`, the open tasks the shell's one
// `tasks.listing` feed (tasksPulse `subscribeListing`). Both were timer polls; neither fetches now. Driven through a
// scripted events client (`setEventsClientForTests`), never `mock.module` (process-wide in bun) or a stubbed fetch.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { installDomShim } from "@platform/lib/testDomShim";
import { setEventsClientForTests } from "@platform/lib/events";
import type { Task } from "@platform/lib/api";

installDomShim();
const { HOME_BOTS_PARAMS, useHomeBots, useHomeTasks } = await import("./data");
const { resetListingFeedForTests } = await import("@shell/tasksPulse");
type State<T> = { data: T | null; error: string | null; retry: () => void };

interface Sub { topic: string; params: unknown; cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void; opts?: { hiddenOk?: boolean }; live: boolean }
let subs: Sub[] = [];
let resyncs: { topic: string; params: unknown }[] = [];
const live = (topic: string) => subs.filter((s) => s.live && s.topic === topic);
const push = (topic: string, snap: unknown, delta: unknown = null, meta: Record<string, unknown> = { gen: null }) =>
  act(() => { for (const s of live(topic)) s.cb(snap, delta, meta); });

beforeEach(() => {
  subs = []; resyncs = [];
  resetListingFeedForTests();
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Sub["cb"], opts?: Sub["opts"]) => {
      const s: Sub = { topic, params, cb, opts, live: true };
      subs.push(s);
      return () => { s.live = false; };
    }) as never,
    resync: ((topic: string, params: unknown) => { resyncs.push({ topic, params }); return true; }) as never,
  });
});
afterEach(() => {
  setEventsClientForTests(null);
  resetListingFeedForTests();
});

function mount<T>(hook: () => State<T>): { get: () => State<T>; r: ReactTestRenderer } {
  let latest!: State<T>;
  function Harness() { latest = hook(); return null; }
  let r!: ReactTestRenderer;
  act(() => { r = create(<Harness />); });
  return { get: () => latest, r };
}

const bot = (id: string, status = "idle") => ({ id, name: id, status, seq: 0, events: [] });
const task = (key: string, status: string, kind = "chat") => ({ key, status, kind }) as unknown as Task;

describe("useHomeBots", () => {
  test("subscribes to `bots` while mounted (hidden-ok); snapshots and deltas both carry the whole list", () => {
    const { get, r } = mount(useHomeBots);
    expect(live("bots").length).toBe(1);
    expect(live("bots")[0].params).toEqual(HOME_BOTS_PARAMS);
    expect(live("bots")[0].opts).toEqual({ hiddenOk: true });
    expect(get().data).toBeNull();
    push("bots", { bots: [bot("a")], ts: 1 });
    expect(get().data?.map((b) => b.id)).toEqual(["a"]);
    push("bots", null, { bots: [bot("a", "running"), bot("b")], ts: 2 });
    expect(get().data?.map((b) => `${b.id}:${b.status}`)).toEqual(["a:running", "b:idle"]);
    act(() => r.unmount());
    expect(live("bots").length).toBe(0);
  });

  test("a refusal keeps the list shown and sets the error; retry clears both and resyncs, never fetches", () => {
    const { get } = mount(useHomeBots);
    push("bots", { bots: [bot("a")], ts: 1 });
    push("bots", null, null, { error: "Worker down", status: 500 });
    expect(get().error).toBe("Worker down");
    expect(get().data?.map((b) => b.id)).toEqual(["a"]);
    act(() => get().retry());
    expect(get().data).toBeNull();
    expect(get().error).toBeNull();
    expect(resyncs).toEqual([{ topic: "bots", params: HOME_BOTS_PARAMS }]);
    push("bots", { bots: [bot("b")], ts: 2 });
    expect(get().data?.map((b) => b.id)).toEqual(["b"]);
  });
});

describe("useHomeTasks", () => {
  test("rides the shared listing feed: everything but archived and drafts (done included), with the counts it left out", () => {
    const { get } = mount(useHomeTasks);
    expect(live("tasks.listing").length).toBe(1);
    push("tasks.listing", { tasks: [task("a", "running"), task("b", "done"), task("c", "archived"), task("d", "idle", "draft"), task("e", "waiting")] });
    expect(get().data?.tasks.map((t) => t.key).sort()).toEqual(["a", "b", "e"]);
    expect(get().data?.drafts).toBe(1);
    expect(get().data?.archived).toBe(1);
  });

  test("two widgets on Home share ONE listing subscription", () => {
    mount(useHomeTasks);
    mount(useHomeTasks);
    expect(live("tasks.listing").length).toBe(1);
  });

  test("a failed read keeps the rows shown and sets the error; retry clears both and asks the feed again", async () => {
    const { get } = mount(useHomeTasks);
    push("tasks.listing", { tasks: [task("a", "running")] });
    push("tasks.listing", null, null, { error: "nope", status: 500 });
    expect(get().error).toBe("Couldn't load tasks.");
    expect(get().data?.tasks.map((t) => t.key)).toEqual(["a"]);
    act(() => get().retry());
    expect(get().data).toBeNull();
    expect(get().error).toBeNull();
    await act(async () => { await Promise.resolve(); });
    expect(resyncs.map((x) => x.topic)).toEqual(["tasks.listing"]);
  });
});
