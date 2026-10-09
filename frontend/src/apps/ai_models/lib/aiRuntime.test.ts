// The runtime hub: one subscription to `ai.runtime` however many surfaces
// read it (the page and the sidebar dot), fed by the fake events client —
// the real one has no socket in bun and never calls back.
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import type { AiRuntime } from "@platform/lib/api";
import { setEventsClientForTests } from "@platform/lib/events";
import { aiRuntimeSettled, isBusy, publishAiRuntime, refreshAiRuntime, useAiRuntime } from "./aiRuntime";

const EMPTY: AiRuntime = {
  runners: [],
  loaded: [],
  downloading: [],
  totalResidentBytes: null,
  memoryCeilingBytes: null,
};

let renderers: ReactTestRenderer[] = [];

afterEach(() => {
  act(() => renderers.forEach((r) => r.unmount()));
  renderers = [];
  setEventsClientForTests(null);
});

function fakeClient() {
  const state = { subscribes: 0, unsubscribes: 0, resyncs: 0, topics: [] as string[] };
  let push: ((snap: unknown) => void) | null = null;
  setEventsClientForTests({
    subscribe: ((topic: string, _p: unknown, cb: (s: unknown, d: unknown, m: Record<string, unknown>) => void) => {
      state.subscribes += 1;
      state.topics.push(topic);
      push = (snap) => cb(snap, null, { gen: null });
      return () => {
        state.unsubscribes += 1;
        push = null;
      };
    }) as never,
    resync: () => {
      state.resyncs += 1;
      return true;
    },
  });
  return { state, push: (snap: unknown) => push?.(snap) };
}

function Reader({ seen }: { seen: (r: AiRuntime) => void }) {
  seen(useAiRuntime());
  return null;
}

function mount(seen: (r: AiRuntime) => void): ReactTestRenderer {
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Reader, { seen }));
  });
  renderers.push(r);
  return r;
}

test("isBusy counts a weights-only download as busy, and a loading model too", () => {
  expect(isBusy(EMPTY)).toBe(false);
  expect(isBusy({ ...EMPTY, downloading: [{} as never] })).toBe(true);
  expect(isBusy({ ...EMPTY, loaded: [{ state: "loading" } as never] })).toBe(true);
  expect(isBusy({ ...EMPTY, loaded: [{ state: "ready" } as never, { state: "error" } as never] })).toBe(false);
});

test("two readers share ONE subscription to `ai.runtime`; it closes with the last reader", () => {
  const { state, push } = fakeClient();
  const seen: AiRuntime[] = [];
  mount((r) => seen.push(r));
  mount((r) => seen.push(r));
  expect(state.subscribes).toBe(1);
  expect(state.topics).toEqual(["ai.runtime"]);

  const snap: AiRuntime = { ...EMPTY, totalResidentBytes: 42 };
  act(() => push(snap));
  expect(seen.slice(-2).every((r) => r.totalResidentBytes === 42)).toBe(true);
  expect(aiRuntimeSettled()).toBe(true);

  act(() => renderers[0].unmount());
  expect(state.unsubscribes).toBe(0);
  act(() => renderers[1].unmount());
  renderers = [];
  expect(state.unsubscribes).toBe(1);
});

test("publishAiRuntime sets locally; refreshAiRuntime is one resync, never a fetch", () => {
  const { state } = fakeClient();
  let latest: AiRuntime = EMPTY;
  mount((r) => {
    latest = r;
  });
  act(() => publishAiRuntime({ ...EMPTY, totalResidentBytes: 7 }));
  expect(latest.totalResidentBytes).toBe(7);
  refreshAiRuntime();
  expect(state.resyncs).toBe(1);
  expect(state.subscribes).toBe(1);
});
