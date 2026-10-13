// The FDA store's transport: one subscription to `fda` however many surfaces
// listen (the strips, the wizard step), fed by the fake events client — the
// real one has no socket in bun and never calls back.
import { afterEach, expect, test } from "bun:test";
import type { FdaState } from "@platform/lib/api";
import { setEventsClientForTests } from "@platform/lib/events";
import { _resetFdaStore, getFda, pokeFda, refresh, seedFda, subscribeFda } from "./fda";

afterEach(() => {
  setEventsClientForTests(null);
  _resetFdaStore();
});

function fakeClient() {
  const state = { subscribes: 0, unsubscribes: 0, resyncs: 0, keys: [] as [string, unknown][] };
  let cb: ((s: unknown, d: unknown, m: Record<string, unknown>) => void) | null = null;
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, c: typeof cb) => {
      state.subscribes += 1;
      state.keys.push([topic, params]);
      cb = c;
      return () => {
        state.unsubscribes += 1;
        cb = null;
      };
    }) as never,
    resync: () => {
      state.resyncs += 1;
      return true;
    },
  });
  return {
    state,
    push: (frame: unknown) => cb?.(frame, null, { gen: null }),
    refuse: () => cb?.(null, null, { error: "down", status: 503 }),
  };
}

const denied: FdaState = { granted: false, pending_relaunch: false, denied: true } as FdaState;

test("listeners share ONE subscription to `fda`, opened by the first and closed by the last", () => {
  const { state, push } = fakeClient();
  let wakes = 0;
  const offA = subscribeFda(() => wakes++);
  const offB = subscribeFda(() => wakes++);
  expect(state.subscribes).toBe(1);
  expect(state.keys).toEqual([["fda", {}]]);
  expect(getFda()).toBe(undefined);

  push({ fda: denied });
  expect(getFda()?.denied).toBe(true);
  expect(wakes).toBe(2);

  // The server has no `fda` field: not offered, and the store says null.
  push({});
  expect(getFda()).toBe(null);

  offA();
  expect(state.unsubscribes).toBe(0);
  offB();
  expect(state.unsubscribes).toBe(1);
});

test("a frame with the same state by value wakes nobody; a refusal keeps the last", () => {
  const { push, refuse } = fakeClient();
  let wakes = 0;
  subscribeFda(() => wakes++);
  push({ fda: denied });
  push({ fda: { ...denied } });
  expect(wakes).toBe(1);
  refuse();
  expect(getFda()?.denied).toBe(true);
});

test("seedFda fills an empty store only; refresh/pokeFda are one resync each and refresh answers what is held", async () => {
  const { state } = fakeClient();
  seedFda(denied);
  expect(getFda()?.denied).toBe(true);
  seedFda(undefined);
  expect(getFda()?.denied).toBe(true);
  subscribeFda(() => {});
  expect(await refresh()).toEqual(denied);
  expect(state.resyncs).toBe(1);
  pokeFda();
  expect(state.resyncs).toBe(2);
  expect(state.subscribes).toBe(1);
});
