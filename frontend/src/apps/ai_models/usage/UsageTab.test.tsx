// The Usage tab's data feed: `ai.metrics {minutes}` on the events bus, not a
// timer. Frames come from a scripted events client (`setEventsClientForTests`,
// never `mock.module`, which is process-wide in bun).
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { default: UsageTab } = await import("@apps/ai_models/usage/UsageTab");
const { setEventsClientForTests } = await import("@platform/lib/events");

type Frame = (snap: unknown, delta: unknown, meta: Record<string, unknown>) => void;
interface Sub {
  topic: string;
  params: unknown;
  cb: Frame;
  open: boolean;
}

function fakeEvents() {
  const subs: Sub[] = [];
  setEventsClientForTests({
    subscribe: ((topic: string, params: unknown, cb: Frame) => {
      const sub: Sub = { topic, params, cb, open: true };
      subs.push(sub);
      return () => {
        sub.open = false;
      };
    }) as never,
    resync: () => true,
  });
  return {
    subs,
    open: () => subs.filter((x) => x.open),
    push: (snap: unknown) => {
      for (const x of subs) if (x.open) x.cb(snap, null, { gen: null });
    },
    fail: (error: string) => {
      for (const x of subs) if (x.open) x.cb(null, null, { error, status: 500 });
    },
  };
}

afterEach(() => setEventsClientForTests(null));

const ZERO = { completions: 0, input_tokens: null, output_tokens: 0, failures: 0, seconds: null, tokens_per_second: null };
function usage(minutes: number, output: number) {
  const counts = { ...ZERO, completions: output ? 1 : 0, output_tokens: output };
  return {
    since: 1000,
    now: 2000,
    bucket_seconds: 10,
    window_minutes: minutes,
    retention_minutes: 60,
    last_completion_at: null,
    totals: counts,
    window: counts,
    tiers: { claude: ZERO, local: counts },
    failure_types: [],
    models: [],
    buckets: [],
  };
}

function text(r: ReactTestRenderer): string {
  return JSON.stringify(r.toJSON());
}

async function mount() {
  let r!: ReactTestRenderer;
  await act(async () => {
    r = create(createElement(UsageTab));
  });
  return r;
}

test("opens ai.metrics for the default 15-minute window and renders its snapshot", async () => {
  const bus = fakeEvents();
  const r = await mount();
  expect(bus.open().map((x) => [x.topic, x.params])).toEqual([["ai.metrics", { minutes: 15 }]]);
  await act(async () => {
    bus.push(usage(15, 4321));
  });
  expect(text(r)).toContain("4,321");
  await act(async () => r.unmount());
  expect(bus.open()).toEqual([]);
});

test("an error frame shows the GET's error and keeps the last good answer", async () => {
  const bus = fakeEvents();
  const r = await mount();
  await act(async () => {
    bus.push(usage(15, 777));
  });
  await act(async () => {
    bus.fail("metrics unavailable");
  });
  expect(text(r)).toContain("metrics unavailable");
  expect(text(r)).toContain("777");
  // The next good snapshot clears the banner.
  await act(async () => {
    bus.push(usage(15, 778));
  });
  expect(text(r)).not.toContain("metrics unavailable");
  await act(async () => r.unmount());
});

test("picking another window re-keys the subscription; the old one closes", async () => {
  const bus = fakeEvents();
  const r = await mount();
  await act(async () => {
    bus.push(usage(15, 5));
  });
  const five = r.root.findAll((n) => n.type === "button" && n.props.children?.[0] === 5)[0]!;
  await act(async () => {
    five.props.onClick();
  });
  expect(bus.open().map((x) => [x.topic, x.params])).toEqual([["ai.metrics", { minutes: 5 }]]);
  expect(bus.subs.length).toBe(2);
  await act(async () => r.unmount());
});

test("no timer drives the feed", () => {
  const src = readFileSync(join(import.meta.dir, "UsageTab.tsx"), "utf8");
  expect(src).not.toContain("setTimeout");
  expect(src).not.toContain("setInterval");
  expect(src).not.toContain("getAiUsage");
});
