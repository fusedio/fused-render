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
import { STALE_CLEAR_MS } from "@platform/lib/instant-search";

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

function mount(): { renderer: ReactTestRenderer; input: () => any; unmount: () => void } {
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
    // Unmount INSIDE act(): effect cleanups (the pending timers, the
    // document listener) must run in the same batched world the rest of the
    // test drives, or React can warn about — or in practice mis-schedule —
    // work outside act().
    unmount: () => act(() => renderer.unmount()),
  };
}

function type(box: { input: () => any }, value: string): Promise<void> {
  return flush(() => box.input().props.onChange({ target: { value } }));
}

/** Elements carrying `cls` among possibly several space-separated classes —
 * `findAllByProps` does exact string equality, which a compound className
 * (e.g. "fh-result-icon fh-ai-glyph") never satisfies. */
function findByClass(box: { renderer: ReactTestRenderer }, cls: string): unknown[] {
  return box.renderer.root.findAll(
    (n) =>
      typeof n.props?.className === "string" && n.props.className.split(" ").includes(cls),
  );
}

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

const hit = (rel: string) => ({
  rel, is_dir: false, size: 1, mtime: 1, score: 10, longest_run: 3, tier: 1, depth: 1,
});

/** The result note's flattened text, kbd/span children included. */
function noteText(box: { renderer: ReactTestRenderer }): string {
  const node = box.renderer.root.findByProps({ className: "fh-result-note" });
  const walk = (n: unknown): string => {
    if (typeof n === "string") return n;
    if (Array.isArray(n)) return n.map(walk).join("");
    if (n && typeof n === "object" && "children" in (n as Record<string, unknown>)) {
      return walk((n as { children: unknown }).children);
    }
    return "";
  };
  return walk(node);
}

describe("MIN_QUERY_CHARS: nothing is asked below it", () => {
  test("one character issues no indexRank", async () => {
    const box = mount();
    await type(box, "a");
    // The idle warm on mount fires its own indexRank(WARM_QUERY); give it a
    // beat to land so it cannot be mistaken for a query-driven call.
    expect(rankCalls.filter((c) => c.q === "a")).toHaveLength(0);
    box.unmount();
  });

  test("two characters ask, at the leading edge", async () => {
    const box = mount();
    await type(box, "ab");
    expect(rankCalls.filter((c) => c.q === "ab")).toHaveLength(1);
    box.unmount();
  });

  test('the note reads "Keep typing…" under the threshold', async () => {
    const box = mount();
    await type(box, "a");
    const note = box.renderer.root.findByProps({ className: "fh-result-note" });
    expect(note.children.join("")).toContain("Keep typing");
    box.unmount();
  });

  test("no result list (and so no AI row) renders under the threshold", async () => {
    const box = mount();
    await type(box, "a");
    expect(box.renderer.root.findAllByProps({ id: "fh-result-list" })).toHaveLength(0);
    box.unmount();
  });
});

describe("stale rows: narrow first, clear only if narrowing empties out", () => {
  test("an extending query narrows the held rows with no round trip, and survives the deadline", async () => {
    const box = mount();
    await type(box, "form");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ hits: [hit("formula.txt"), hit("format.md")], total: 2 })));

    // Extend the query; the second request is left hanging.
    await flush(() => box.input().props.onChange({ target: { value: "forma" } }));
    await flush(() => clock.advance(200)); // past the trailing debounce
    expect(rankCalls).toHaveLength(2);
    expect(box.renderer.root.findAllByProps({ className: "fh-result-name" }).length)
      .toBeGreaterThan(0); // narrowed rows are on screen already, no round trip needed

    // Run the clock well past the staleness deadline: narrowing left rows, so
    // they must NOT be thrown away.
    await flush(() => clock.advance(1_000));
    expect(box.renderer.root.findAllByProps({ className: "fh-result-name" }).length)
      .toBeGreaterThan(0);
    box.unmount();
  });

  test("an unrelated query narrows to nothing, and the deadline drops to a bare 'Searching…'", async () => {
    const box = mount();
    await type(box, "form");
    await flush(() => rankCalls[0].reply.resolve(
      answer({ hits: [hit("formula.txt"), hit("format.md")], total: 2 })));
    expect(noteText(box)).not.toContain("Searching");

    // A paste-over: nothing held matches this at all.
    await flush(() => box.input().props.onChange({ target: { value: "zzzqqq" } }));
    await flush(() => clock.advance(200));
    expect(rankCalls).toHaveLength(2);

    // Before the deadline: still the previous (stale) note, not "Searching…".
    expect(noteText(box)).not.toBe("Searching…");

    await flush(() => clock.advance(STALE_CLEAR_MS + 50));
    expect(noteText(box)).toBe("Searching…");
    box.unmount();
  });
});

describe("a query that is really an address (section 7)", () => {
  test("a resolving absolute path issues no indexRank and offers no AI row", async () => {
    const box = mount();
    await type(box, "/tmp/report.csv");
    // No rank request for a literal address — 7c skips it entirely.
    expect(rankCalls).toHaveLength(0);
    expect(statCalls.map((c) => c.path)).toEqual(["/tmp/report.csv"]);

    await flush(() => statCalls[0].reply.resolve({
      path: "/tmp/report.csv", name: "report.csv", is_dir: false, size: 1, mtime: 1, templates: [],
    }));
    // The Open row renders in place of any AI row.
    expect(findByClass(box, "fh-ai-glyph")).toHaveLength(0);
    expect(box.renderer.root.findAllByProps({ id: "fh-row-0" }).length).toBeGreaterThan(0);
    box.unmount();
  });

  test("suppresses the AI row even while the stat is still in flight", async () => {
    const box = mount();
    await type(box, "/tmp/still-checking");
    expect(findByClass(box, "fh-ai-glyph")).toHaveLength(0);
    box.unmount();
  });

  test("suppresses the AI row even when the address does not resolve", async () => {
    const box = mount();
    await type(box, "/tmp/does-not-exist");
    await flush(() => statCalls[0].reply.reject(new Error("not found")));
    // Falls through to a normal search (7d) — but still no AI row (7e).
    expect(rankCalls.map((c) => c.q)).toEqual(["/tmp/does-not-exist"]);
    expect(findByClass(box, "fh-ai-glyph")).toHaveLength(0);
    box.unmount();
  });

  test("a plain query still gets its AI row back", async () => {
    const box = mount();
    await type(box, "readme");
    expect(findByClass(box, "fh-ai-glyph").length).toBeGreaterThan(0);
    box.unmount();
  });
});
