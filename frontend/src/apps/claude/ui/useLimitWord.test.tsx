// THE CHAT HEADER'S PAUSED WORD, READ FOR THIS CHAT'S OWN ROW (Bugbot PR #1124).
//
// The word used to come off `sched.rec`, which the scheduled-message block only
// fetches while a card is drawn. A session that hit the plan's usage limit with
// nothing waiting behind it — the ordinary case — therefore had no row at all,
// and the one surface the reader is standing on said nothing about the one thing
// that had stopped their chat.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { createElement } from "react";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

const { useLimitWord, LIMIT_REFRESH_MS } = await import("./useLimitWord");
const { TASKS_CHANGED_EVENT } = await import("@platform/lib/tasksChanged");

const KEY = "sess-a";

/** 4:00 AM on the machine running this suite, built from local parts so the
 *  assertion is about the WORDS and never about the zone. */
const AT_4AM = Math.floor(new Date(2026, 8, 12, 4, 0, 0, 0).getTime() / 1000);

/** The shim's `window` is a no-op for events, and half of what this hook does IS
 *  an event listener — so the global window gets a real, tiny registry for the
 *  length of a test (the technique `useTaskId.test.tsx` uses, for its reason). */
function liveWindowEvents(): () => void {
  const w = globalThis.window as unknown as {
    addEventListener: unknown;
    removeEventListener: unknown;
    dispatchEvent: unknown;
  };
  const was = {
    add: w.addEventListener,
    remove: w.removeEventListener,
    fire: w.dispatchEvent,
  };
  const bus = new Map<string, Set<(ev: Event) => void>>();
  w.addEventListener = (type: string, fn: (ev: Event) => void) => {
    const set = bus.get(type) ?? new Set();
    set.add(fn);
    bus.set(type, set);
  };
  w.removeEventListener = (type: string, fn: (ev: Event) => void) => {
    bus.get(type)?.delete(fn);
  };
  w.dispatchEvent = (ev: Event) => {
    for (const fn of [...(bus.get(ev.type) ?? [])]) fn(ev);
    return true;
  };
  return () => {
    w.addEventListener = was.add;
    w.removeEventListener = was.remove;
    w.dispatchEvent = was.fire;
  };
}

const mounted: ReactTestRenderer[] = [];
const undo: Array<() => void> = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
  for (const off of undo.splice(0)) off();
});

interface Rig {
  word(): string;
  reads(): number;
  serve(tasks: Array<Record<string, unknown>>): void;
  advance(ms: number): void;
  /** Ring `tasks-changed` and flush whatever it starts. */
  announce(): Promise<void>;
  /** Let a deferred read's timer fire. */
  settle(): Promise<void>;
}

async function mount(
  tasks: Array<Record<string, unknown>>,
  taskKey = KEY,
): Promise<Rig> {
  let served = tasks;
  let reads = 0;
  let clock = 1_000_000;
  let out = "";
  const deps = {
    getTasks: async () => {
      reads += 1;
      return { tasks: served };
    },
    now: () => clock,
    floorMs: 20,
  };
  function Probe() {
    out = useLimitWord(taskKey, deps);
    return null;
  }
  let renderer!: ReactTestRenderer;
  await act(async () => {
    renderer = create(createElement(Probe));
  });
  mounted.push(renderer);
  return {
    word: () => out,
    reads: () => reads,
    serve(next) {
      served = next;
    },
    advance(ms) {
      clock += ms;
    },
    async announce() {
      await act(async () => {
        window.dispatchEvent(new Event(TASKS_CHANGED_EVENT));
      });
    },
    async settle() {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 40));
      });
    },
  };
}

test("the floor is the same five seconds the chat's own row read has", () => {
  expect(LIMIT_REFRESH_MS).toBe(5000);
});

test("says `paused · resumes` for a limited session with NO card drawn", async () => {
  undo.push(liveWindowEvents());
  // `queued`, which is what a limited session whose folder is held is filed as —
  // and the status this read deliberately does not ask about.
  const h = await mount([
    { key: KEY, status: "queued", blocked_reason: "usage_limit", resumes_at: AT_4AM },
  ]);
  expect(h.reads()).toBe(1);
  expect(h.word()).toBe("paused · resumes 4:00 AM");
});

test("says nothing for an ordinary chat, or for a row that is not here", async () => {
  undo.push(liveWindowEvents());
  const plain = await mount([{ key: KEY, status: "in_progress" }]);
  expect(plain.word()).toBe("");
  const elsewhere = await mount([
    { key: "somebody-else", status: "blocked", blocked_reason: "usage_limit" },
  ]);
  expect(elsewhere.word()).toBe("");
});

test("re-reads on `tasks-changed` — which is what the comeback's POST rings", async () => {
  undo.push(liveWindowEvents());
  const h = await mount([{ key: KEY, status: "in_progress" }]);
  expect(h.word()).toBe("");

  // The limit hits: the run controller schedules the comeback and announces.
  h.serve([
    { key: KEY, status: "blocked", blocked_reason: "usage_limit", resumes_at: AT_4AM },
  ]);
  h.advance(5);
  await h.announce();
  // Inside the floor, so the read is DEFERRED rather than dropped…
  expect(h.reads()).toBe(1);
  await h.settle();
  expect(h.reads()).toBe(2);
  expect(h.word()).toBe("paused · resumes 4:00 AM");
});

test("a burst of announcements is one read", async () => {
  undo.push(liveWindowEvents());
  const h = await mount([{ key: KEY, status: "in_progress" }]);
  for (let i = 0; i < 5; i += 1) await h.announce();
  await h.settle();
  expect(h.reads()).toBe(2);
});

test("a chat with no key at all reads nothing", async () => {
  undo.push(liveWindowEvents());
  const h = await mount([{ key: KEY, status: "blocked", blocked_reason: "usage_limit" }], "");
  expect(h.reads()).toBe(0);
  expect(h.word()).toBe("");
});
