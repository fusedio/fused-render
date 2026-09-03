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
import { restoreGlobal } from "@platform/lib/testDomShim";

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
/** Every `history.pushState(..., url)` the router's `navigate()` makes — the
 * observable trace of a navigation, since `navigate` itself is a frozen ES
 * module export this file cannot spy on (see the file-header comment on why
 * `mock.module` is out) and `window.dispatchEvent`/`history.pushState` are
 * plain stubbed globals same as `fetch` above. */
const navPushes: string[] = [];

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

// What `beforeEach` displaced, so `afterEach` can put it back.
let savedHistory: unknown;
let savedDocument: unknown;

beforeEach(() => {
  rankCalls.length = 0;
  statCalls.length = 0;
  navPushes.length = 0;
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
  // `navigate()` (router.ts) also fires `window.dispatchEvent(new
  // Event(NAV_EVENT))` — Clock's stubbed `window` has no such method, so
  // anything that reaches `navigate()` (an Enter that commits a resolved
  // address) throws without this.
  (globalThis as unknown as { window: Record<string, unknown> }).window.dispatchEvent = () => true;
  //
  // SAVED and put back in `afterEach`, not deleted: `history` comes from the
  // preloaded shared shim (platform/lib/testDomShim.ts) and the files that
  // import router.ts statically need it standing when their module graph
  // evaluates, whatever ran before them.
  savedHistory = (globalThis as Record<string, unknown>).history;
  savedDocument = (globalThis as Record<string, unknown>).document;
  (globalThis as Record<string, unknown>).history = {
    state: null,
    replaceState: () => {},
    pushState: (_state: unknown, _title: string, url?: string | URL | null) => {
      if (url) navPushes.push(String(url));
    },
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
  restoreGlobal("history", savedHistory);
  restoreGlobal("document", savedDocument);
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

  test("an unrelated query narrows to nothing: the note holds the last settled count instead of flashing \"Searching…\"", async () => {
    const box = mount();
    await type(box, "form");
    await flush(() => rankCalls[0].resolve(
      answer({ hits: [hit("formula.txt"), hit("format.md")], total: 2 })));
    expect(noteText(box)).not.toContain("Searching");
    expect(noteText(box)).toContain("2 matches");

    // A paste-over: nothing held matches this at all, so `hits` narrows to
    // empty — but the note reads from the last SETTLED answer (`noteAnswer`,
    // home-search.ts), not from `hits`, so it keeps reading the held count
    // (now with a "+", since `behind` says more could be out there for this
    // query than the held answer ever had a chance to include) rather than
    // reverting to "Searching…" for a query that has not actually failed to
    // find anything yet.
    await flush(() => box.input().props.onChange({ target: { value: "zzzqqq" } }));
    await flush(() => clock.advance(200));
    expect(rankCalls).toHaveLength(2);

    // Before the deadline: the held note is unchanged but for that "+".
    expect(noteText(box)).toContain("2+ matches");
    // The rows themselves are still `behind` (dimmed): the held answer is
    // for "form", not yet given up on.
    expect(box.renderer.root.findByProps({ id: "fh-result-list" }).props.className)
      .toContain("is-stale");

    await flush(() => clock.advance(STALE_CLEAR_MS + 50));
    // Past the deadline `answer` itself drops to null, so `behind` (which
    // reads `answer`, not the held note) goes false along with the dimming —
    // the "+" drops with it — but the count keeps reading the held total:
    // `noteAnswer` only ever changes when a query actually settles, and
    // nothing has for "zzzqqq" yet.
    expect(noteText(box)).toContain("2 matches");
    expect(box.renderer.root.findByProps({ id: "fh-result-list" }).props.className)
      .not.toContain("is-stale");
    box.unmount();
  });

  test("the count note holds the last settled total while rows narrow underneath it", async () => {
    const box = mount();
    await type(box, "form");
    // A broad first answer: the note claims 137 matches.
    await flush(() => rankCalls[0].resolve(
      answer({
        hits: [hit("formula.txt"), hit("format.md"), hit("formal.doc")],
        total: 137,
        truncated: true,
      }),
    ));
    expect(noteText(box)).toContain("137");

    // Extend to a query only ONE of the three held hits still matches
    // ("formula.txt" — the others lack a "u"). The second request is left
    // hanging, so this is all narrowing, no round trip.
    await flush(() => box.input().props.onChange({ target: { value: "formu" } }));
    await flush(() => clock.advance(200));
    // Only the file row with an href is a FILE hit — the AI row also carries
    // `.fh-result-name` (its "Search with AI" label), so counting that class
    // alone would double-count it. The rows on screen DO narrow with the
    // query (`narrowAnswer`) — it is only the count note that holds still.
    expect(box.renderer.root.findAll((n) => typeof n.props?.href === "string")).toHaveLength(1);
    // The note reads the last SETTLED answer (`noteAnswer`), not the
    // narrowed row count, so it keeps reporting 137 rather than rewriting
    // itself to a number that describes a search that was never sent for
    // "formu" at all — the dimmed rows already say this is stale.
    expect(noteText(box)).toContain("137");
    box.unmount();
  });

  test("typing a second query never flips the note to \"Searching…\" once a count has been shown", async () => {
    const box = mount();
    await type(box, "form");
    await flush(() => rankCalls[0].resolve(
      answer({ hits: [hit("formula.txt"), hit("format.md")], total: 2 })));
    expect(noteText(box)).toContain("2 matches");

    // Every keystroke of a second query, with the round trip left hanging —
    // at no point should the note revert to "Searching…": that number is
    // the last thing settled, and it stays on screen until a new one lands.
    for (const value of ["forma", "formal", "formal "]) {
      await flush(() => box.input().props.onChange({ target: { value } }));
      await flush(() => clock.advance(200));
      expect(noteText(box)).not.toBe("Searching…");
    }
    box.unmount();
  });
});

describe("the latency readout", () => {
  test("reports the round-trip time next to the count", async () => {
    const box = mount();
    await type(box, "readme");
    clock.advance(87);
    await flush(() => rankCalls[0].resolve(answer({ hits: [hit("readme.md")], total: 1 })));
    expect(noteText(box)).toContain("87 ms");
    box.unmount();
  });

  test("a memoised answer (backspace) keeps the elapsed time it was measured with", async () => {
    const box = mount();
    await type(box, "readme");
    clock.advance(120);
    await flush(() => rankCalls[0].resolve(answer({ hits: [hit("readme.md")], total: 1 })));
    expect(noteText(box)).toContain("120 ms");

    // Extend, then backspace back to the memoised query — no new round trip,
    // so the readout must still read the original measurement, not ~0ms.
    await flush(() => box.input().props.onChange({ target: { value: "readmex" } }));
    await flush(() => box.input().props.onChange({ target: { value: "readme" } }));
    expect(rankCalls.filter((c) => c.q === "readme")).toHaveLength(1); // no re-ask
    expect(noteText(box)).toContain("120 ms");
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

  test("the note does not describe a search that was never sent while the stat is in flight", async () => {
    const box = mount();
    // A normal query first, so a stale answer with a real count exists to
    // wrongly fall back to.
    await type(box, "readme");
    await flush(() => rankCalls[0].resolve(
      answer({ hits: [hit("readme.md")], total: 137, truncated: true })));
    expect(noteText(box)).toContain("137");

    // Paste a path over it: suppressRank holds the rank request back
    // entirely (no request goes out for "/tmp/report.csv"), and the stat
    // hasn't answered yet (showOpenRow is false) — so the note must not fall
    // through to describing the OLD answer.
    await flush(() => box.input().props.onChange({ target: { value: "/tmp/report.csv" } }));
    expect(statCalls).toHaveLength(1);
    expect(rankCalls).toHaveLength(1); // no second rank request
    expect(noteText(box)).not.toContain("137");
    box.unmount();
  });

  test("the stale-clear deadline still fires while suppressRank holds the rank request back", async () => {
    // `pending` is false on the suppressed-rank path (the rank effect
    // early-returns before ever setting it) — the deadline effect used to
    // gate on `pending`, which never fires here, so the held answer (and its
    // `is-stale` dimming) stayed behind indefinitely instead of clearing once
    // narrowing empties it out, same as the pending-request path does.
    const box = mount();
    await type(box, "readme");
    await flush(() => rankCalls[0].resolve(
      answer({ hits: [hit("readme.md")], total: 137, truncated: true })));

    await flush(() => box.input().props.onChange({ target: { value: "/tmp/report.csv" } }));
    expect(statCalls).toHaveLength(1); // the stat is issued but left hanging
    expect(box.renderer.root.findByProps({ id: "fh-result-list" }).props.className)
      .toContain("is-stale");

    await flush(() => clock.advance(STALE_CLEAR_MS + 50));
    expect(box.renderer.root.findByProps({ id: "fh-result-list" }).props.className)
      .not.toContain("is-stale");
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

function pressEnter(box: { input: () => any }): Promise<void> {
  return flush(() =>
    box.input().props.onKeyDown({ key: "Enter", preventDefault: () => {} }),
  );
}

describe("Enter while a pasted path's stat is still resolving (section 7 paste-and-go)", () => {
  // `submitRow` has nothing to commit here: `suppressRank` holds ranking
  // back (no rank request, no file rows), `showOpenRow` is false (the stat
  // hasn't answered yet) and the AI row is suppressed too (`address !==
  // null`). Enter used to be a silent no-op in this exact window — which is
  // precisely the paste-and-go gesture the address feature exists for.
  test("commits the address once the in-flight stat resolves", async () => {
    const box = mount();
    await type(box, "/tmp/report.csv");
    expect(statCalls).toHaveLength(1);
    await pressEnter(box);
    expect(navPushes).toHaveLength(0); // nothing yet — the stat is still out
    await flush(() =>
      statCalls[0].resolve({
        path: "/tmp/report.csv", name: "report.csv", is_dir: false, size: 1, mtime: 1, templates: [],
      }),
    );
    expect(navPushes.some((u) => u.includes("report.csv"))).toBe(true);
    box.unmount();
  });

  test("does NOT navigate when the stat resolves to missing", async () => {
    const box = mount();
    await type(box, "/tmp/does-not-exist");
    await pressEnter(box);
    await flush(() => statCalls[0].reject());
    expect(navPushes).toHaveLength(0);
    box.unmount();
  });

  test("a superseded stat (aborted by a newer keystroke) still does not navigate", async () => {
    const box = mount();
    await type(box, "/tmp/report.csv");
    await pressEnter(box);
    // Edit the query before the stat comes back — the pending commit must
    // not fire for an address the user has since typed past.
    await flush(() => box.input().props.onChange({ target: { value: "/tmp/other.csv" } }));
    await flush(() =>
      statCalls[0].resolve({
        path: "/tmp/report.csv", name: "report.csv", is_dir: false, size: 1, mtime: 1, templates: [],
      }),
    );
    expect(navPushes).toHaveLength(0);
    box.unmount();
  });
});

/** Whether the AI row's `<kbd>↵</kbd>` Enter-affordance is on screen. Scoped
 * to `.fh-ai-hint` specifically — the page has other `<kbd>`s (the open-row
 * hint, the "↑↓ to pick" caption) that a bare `n.type === "kbd"` search would
 * also match. */
function hasAiEnterHint(box: { renderer: ReactTestRenderer }): boolean {
  const hint = box.renderer.root.findByProps({ className: "fh-ai-hint" });
  return hint.findAll((n) => n.type === "kbd").length > 0;
}

function pressArrow(box: { input: () => any }, dir: "down" | "up"): Promise<void> {
  return flush(() =>
    box.input().props.onKeyDown({
      key: dir === "down" ? "ArrowDown" : "ArrowUp",
      preventDefault: () => {},
    }),
  );
}

describe("the AI row's ↵ hint only claims Enter when Enter runs it", () => {
  // The badge is the strongest affordance in the row; it used to render
  // unconditionally whenever the row wasn't `running`, including while the
  // top FILE hit — not the AI row — was what Enter would actually commit.
  test("absent with file hits showing and the AI row not highlighted", async () => {
    const box = mount();
    await type(box, "readme");
    await flush(() =>
      rankCalls[0].resolve(answer({ hits: [hit("readme.md")], total: 1 })),
    );
    // The top file row pre-selects (the highlight fix); the AI row is on
    // screen but not the active one, so its Enter hint must not be.
    expect(hasAiEnterHint(box)).toBe(false);
    box.unmount();
  });

  test("appears once the highlight reaches the AI row", async () => {
    const box = mount();
    await type(box, "readme");
    await flush(() =>
      rankCalls[0].resolve(answer({ hits: [hit("readme.md")], total: 1 })),
    );
    expect(hasAiEnterHint(box)).toBe(false);
    await pressArrow(box, "down"); // file row 0 -> the AI row (a one-hit list)
    expect(hasAiEnterHint(box)).toBe(true);
    box.unmount();
  });

  test("present in the settled-zero-hit case, where the AI row pre-selects", async () => {
    const box = mount();
    await type(box, "zzzqqqnomatch");
    await flush(() => rankCalls[0].resolve(answer({ hits: [], total: 0 })));
    // No file hits and settled: activeRow pre-selects the AI row unprompted,
    // so its hint should already show without pressing a key.
    expect(hasAiEnterHint(box)).toBe(true);
    box.unmount();
  });
});

describe("the AI row's sticky positioning is on the LIST ITEM, not the button inside it", () => {
  // `position: sticky` is constrained to its element's containing block —
  // here the <li>, whose content box is exactly the <button>'s height once
  // the class carrying `position: sticky; bottom: 0` sits on the button
  // instead: the offset range is zero and the row never actually detaches,
  // scrolling out of view underneath the fold like any other row (silently
  // undoing the whole point of HOME_RESULT_CAP=20 plus a scrolling
  // `.fh-results` — see home-search.ts's own comment on why the cap is safe
  // only because this row is reachable without scrolling). preferences.css
  // must carry `.fh-ai-row`'s sticky/background/border-top rules on the
  // <li>, not the <button>.
  test("the fh-ai-row class is on the <li>, not the <button>", async () => {
    const box = mount();
    await type(box, "zzzqqqnomatch");
    await flush(() => rankCalls[0].resolve(answer({ hits: [], total: 0 })));
    const carriers = box.renderer.root.findAll(
      (n) =>
        typeof n.props?.className === "string" &&
        n.props.className.split(" ").includes("fh-ai-row"),
    );
    expect(carriers.length).toBeGreaterThan(0);
    for (const n of carriers) expect(n.type).toBe("li");
    box.unmount();
  });
});
