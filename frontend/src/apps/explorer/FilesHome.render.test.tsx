// FilesSearch, DRIVEN: a query is typed, a rank/stat reply lands, the clock
// moves past the debounce. Everything here is a SEQUENCE, which a plain
// function test over `home-search.ts` cannot exercise — the component wires
// the pure helpers there to real state and real timers, and that wiring is
// exactly what sections 2 and 7 of the overhaul touch.
//
// react-test-renderer, the same tool hook-harness.ts uses: no DOM, real React,
// real effects. The Clock/flush pair is reused from there rather than
// re-invented — the same virtual-timer shape a per-query round trip needs.
//
// Deliberately NOT `mock.module("@platform/lib/api", ...)`. That looks like
// the obvious way to control indexRank/statPath, and it is exactly what broke
// CI: `mock.module` replaces the module for the WHOLE bun process — every
// FILE, not just this one — and a real ES module namespace export is frozen
// (confirmed directly: assigning to one throws "Attempted to assign to
// readonly property"), so there is no way to patch just the two functions
// this file needs and leave the rest of the module alone. Registering the
// mock AGAIN with the real module as the factory does not reliably undo it
// either — a module that already imported the mocked version (fs-actions.ts,
// loaded fresh by fs-actions.test.ts AFTER this file's restore had already
// run) still came back with a stale/broken binding, which is what turned
// into a passing-locally, hanging-in-CI 5s timeout in a file this diff never
// touches. The REAL functions here are both thin `getJson`/fetch wrappers, so
// this stubs `globalThis.fetch` instead — a plain, unfrozen global — exactly
// the technique fs-actions.test.ts/fs-clipboard.test.ts already use for the
// same reason.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { IndexRankResult, StatResult } from "@platform/lib/api";
import { Clock } from "@apps/explorer/listing/hook-harness";
import { STALE_CLEAR_MS } from "@platform/lib/instant-search";
import { resetFsMutations } from "@platform/lib/index-freshness";

// --- the module boundary: a fetch stub, not a module mock -------------------
interface RankCall {
  root: string;
  q: string;
  resolve: (data: IndexRankResult) => void;
}
interface StatCall {
  path: string;
  resolve: (data: StatResult) => void;
  /** A 404, exactly what the real /api/fs/stat sends for a path that does not
   * exist — not a network-level rejection, so this matches what statPath()
   * actually throws (an HttpError) in that case. */
  reject: () => void;
}
const rankCalls: RankCall[] = [];
const statCalls: StatCall[] = [];

const realFetch = globalThis.fetch;

/** Every indexRank/statPath call becomes an entry in rankCalls/statCalls,
 * settled only when the test calls `.resolve()`/`.reject()` — the same
 * leading-edge control the old Deferred-based mock gave, without touching
 * the module registry at all. */
function fakeFetch(url: string | URL): Promise<Response> {
  const u = String(url);
  if (u.startsWith("/api/index/rank")) {
    const params = new URL(u, "http://localhost").searchParams;
    return new Promise<Response>((settle) => {
      rankCalls.push({
        root: params.get("root") ?? "",
        q: params.get("q") ?? "",
        resolve: (data) => settle(new Response(JSON.stringify(data), { status: 200 })),
      });
    });
  }
  if (u.startsWith("/api/fs/stat")) {
    const params = new URL(u, "http://localhost").searchParams;
    return new Promise<Response>((settle) => {
      statCalls.push({
        path: params.get("path") ?? "",
        resolve: (data) => settle(new Response(JSON.stringify(data), { status: 200 })),
        reject: () =>
          settle(new Response(JSON.stringify({ error: "no such file" }), { status: 404 })),
      });
    });
  }
  throw new Error("FilesHome.render.test.tsx: unexpected fetch " + u);
}

// router.ts reads `location` at MODULE INIT (a legacy /embed/ rewrite), before
// any beforeEach runs — this has to exist before FilesHome (which imports it
// transitively) is ever imported below. It is torn down again in `afterEach`
// (below) exactly like the other globals this file stubs — module-scope
// setup with no matching teardown is what leaked `document` across files the
// first time this file was written (see the afterEach comment).
(globalThis as Record<string, unknown>).location = { pathname: "/explorer", search: "" };

const { FilesSearch } = await import("@apps/explorer/FilesHome");

const HOME = "/Users/me";

const clock = new Clock();

// Every renderer `mount()` creates, so `afterEach` can unmount it
// UNCONDITIONALLY — including when a test's own assertions throw partway
// through and never reach its own `box.unmount()` call. An unmounted-less
// FilesSearch keeps its `subscribeFsMutations`/`subscribeIndexLifecycle`
// listeners registered on those SHARED, module-level Sets
// (platform/lib/index-freshness) for the rest of the process: the next
// unrelated test file to call `noteFsMutation` invokes every listener still
// registered, including this stale one, which is exactly the kind of
// leaked-subscriber failure a "clean" run cannot reproduce locally but CI
// (running every file in one process) can.
const mounted: ReactTestRenderer[] = [];

beforeEach(() => {
  rankCalls.length = 0;
  statCalls.length = 0;
  globalThis.fetch = fakeFetch as typeof fetch;
  // `indexRescanPending` (platform/lib/index-freshness) reads a module-level
  // `mutatedAt` set by real, non-virtual `Date.now()` — a leaked subscriber
  // isn't the only way that module bites a later file; a PREDECESSOR test
  // (anywhere in the process) that called `noteFsMutation` and never reset
  // leaves this app "still indexing" for up to a real minute, and nothing
  // about mounting/unmounting a component clears it. Reset unconditionally
  // so every test here mounts against a known-idle index, whatever ran
  // before it in CI's single bun process.
  resetFsMutations();
  clock.install();
  // Clock.install() sets up `window`/`location`; the real router module also
  // touches `history` (replaceSearch/navigateUrl) and `document` (the
  // "typing anywhere is typing here" redirect), which nothing here calls
  // directly but which module-level or effect code may still reach for.
  (globalThis as Record<string, unknown>).history = {
    state: null,
    replaceState: () => {},
    pushState: () => {},
  };
  (globalThis as Record<string, unknown>).document = {
    addEventListener: () => {},
    removeEventListener: () => {},
  };
});
afterEach(() => {
  while (mounted.length) {
    const renderer = mounted.pop()!;
    act(() => renderer.unmount());
  }
  globalThis.fetch = realFetch;
  clock.restore();
  delete (globalThis as Record<string, unknown>).history;
  delete (globalThis as Record<string, unknown>).document;
  resetFsMutations();
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
  // Tracked for the unconditional afterEach sweep (above) — removed here on a
  // NORMAL unmount so that sweep does not try to unmount an already-unmounted
  // renderer for every test that reaches its own cleanup.
  mounted.push(renderer);
  return {
    renderer,
    input: () => renderer.root.findByProps({ className: "files-search-input" }),
    // Unmount INSIDE act(): effect cleanups (the pending timers, the
    // document listener) must run in the same batched world the rest of the
    // test drives, or React can warn about — or in practice mis-schedule —
    // work outside act().
    unmount: () => {
      const i = mounted.indexOf(renderer);
      if (i !== -1) mounted.splice(i, 1);
      act(() => renderer.unmount());
    },
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
    await flush(() => rankCalls[0].resolve(
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
    await flush(() => rankCalls[0].resolve(
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

    await flush(() => statCalls[0].resolve({
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
    await flush(() => statCalls[0].reject());
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
