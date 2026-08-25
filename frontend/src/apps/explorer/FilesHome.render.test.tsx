// FilesSearch, DRIVEN: a query is typed, a rank/stat reply lands, the clock
// moves past the debounce. Everything here is a SEQUENCE, which a plain
// function test over `home-search.ts` cannot exercise — the component wires
// the pure helpers there to real state and real timers, and that wiring is
// exactly what sections 2 and 7 of the overhaul touch.
//
// react-test-renderer, the same tool hook-harness.ts uses: no DOM, real React,
// real effects. The Clock/Deferred/flush trio is reused from there rather than
// re-invented — the same virtual-timer shape a per-query round trip needs.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { IndexRankResult, StatResult } from "@platform/lib/api";
import { Clock, Deferred } from "@apps/explorer/listing/hook-harness";

// --- the module boundary -----------------------------------------------------
const rankCalls: { root: string; q: string; reply: Deferred<IndexRankResult> }[] = [];
const statCalls: { path: string; reply: Deferred<StatResult> }[] = [];

// router.ts reads `location` at MODULE INIT (a legacy /embed/ rewrite), before
// any beforeEach runs — this has to exist before FilesHome (which imports it
// transitively) is ever imported below.
(globalThis as Record<string, unknown>).location = { pathname: "/explorer", search: "" };
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: () => {},
  pushState: () => {},
};
// FilesSearch listens on `document` (the "typing anywhere is typing here"
// redirect) and calls `.focus()` on the input ref.
(globalThis as Record<string, unknown>).document = {
  addEventListener: () => {},
  removeEventListener: () => {},
};

const realApi = await import("@platform/lib/api");
mock.module("@platform/lib/api", () => ({
  ...realApi,
  indexRank: (root: string, q: string) => {
    const reply = new Deferred<IndexRankResult>();
    rankCalls.push({ root, q, reply });
    return reply.promise;
  },
  statPath: (path: string) => {
    const reply = new Deferred<StatResult>();
    statCalls.push({ path, reply });
    return reply.promise;
  },
}));

const { FilesSearch } = await import("@apps/explorer/FilesHome");

const HOME = "/Users/me";

function answer(over: Partial<IndexRankResult> = {}): IndexRankResult {
  return {
    covered: true,
    fresh: true,
    reason: "",
    root: HOME,
    hits: [],
    truncated: false,
    total: 0,
    updated: 1,
    age_s: 1,
    ...over,
  };
}

const clock = new Clock();

beforeEach(() => {
  rankCalls.length = 0;
  statCalls.length = 0;
  clock.install();
  // Clock.install() sets up `window`/`location`; the real router module also
  // touches `history` (replaceSearch/navigateUrl), which nothing here calls
  // directly but which module-level code may still reach for.
  (globalThis as Record<string, unknown>).history = {
    state: null,
    replaceState: () => {},
    pushState: () => {},
  };
});
afterEach(() => {
  clock.restore();
  delete (globalThis as Record<string, unknown>).history;
});

/** Run `fn` inside `act` and let any microtasks it releases settle. */
async function flush(fn: () => void = () => {}): Promise<void> {
  await act(async () => {
    fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function mount(): { renderer: ReactTestRenderer; input: () => any } {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(
      createElement(FilesSearch, {
        home: HOME,
        initialQuery: "",
        indexScan: null,
        onActiveChange: () => {},
      }),
    );
  });
  return {
    renderer,
    input: () => renderer.root.findByProps({ className: "files-search-input" }),
  };
}

function type(box: { input: () => any }, value: string): Promise<void> {
  return flush(() => box.input().props.onChange({ target: { value } }));
}

describe("MIN_QUERY_CHARS: nothing is asked below it", () => {
  test("one character issues no indexRank", async () => {
    const box = mount();
    await type(box, "a");
    // The idle warm on mount fires its own indexRank(WARM_QUERY); give it a
    // beat to land so it cannot be mistaken for a query-driven call.
    expect(rankCalls.filter((c) => c.q === "a")).toHaveLength(0);
    box.renderer.unmount();
  });

  test("two characters ask, at the leading edge", async () => {
    const box = mount();
    await type(box, "ab");
    expect(rankCalls.filter((c) => c.q === "ab")).toHaveLength(1);
    box.renderer.unmount();
  });

  test('the note reads "Keep typing…" under the threshold', async () => {
    const box = mount();
    await type(box, "a");
    const note = box.renderer.root.findByProps({ className: "fh-result-note" });
    expect(note.children.join("")).toContain("Keep typing");
    box.renderer.unmount();
  });

  test("no result list (and so no AI row) renders under the threshold", async () => {
    const box = mount();
    await type(box, "a");
    expect(box.renderer.root.findAllByProps({ id: "fh-result-list" })).toHaveLength(0);
    box.renderer.unmount();
  });
});

// Section 7 (below) adds this describe block back once the path-shortcut row
// exists to assert against; landed separately so this file's tests stay green
// commit-by-commit rather than carrying a red one across the gap.
