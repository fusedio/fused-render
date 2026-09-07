// RankedSearchToggle (D720): renders, reflects the persisted value, writes
// on change via a real PUT /api/prefs, and publishes the new value into the
// module-level cache the explorer's two search boxes read
// (ranked-search-pref.ts) — same coverage shape as the pref-threading tests
// in listing/useWalkSearch.render.test.ts, but for the control itself.
//
// A stubbed `globalThis.fetch`, not `mock.module` — `putRankedSearchEnabled`
// is a thin fetch wrapper (api.ts), and `mock.module` replacing the whole
// module process-wide is exactly the pitfall FilesHome.render.test.tsx's own
// header comment documents; this file needs only the one PUT intercepted.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer, type ReactTestRendererJSON } from "react-test-renderer";
import { createElement } from "react";
import type { IndexConfig, IndexStatus, Prefs } from "@platform/lib/api";
import { IndexingPanel, RankedSearchToggle } from "@shell/Indexing";

function fakePrefs(ranked: boolean): Prefs {
  return { indexing: { enabled: true, ranked } } as unknown as Prefs;
}

const realFetch = globalThis.fetch;
let putCalls: { body: unknown }[] = [];
let putResponseRanked = false;

function fakeFetch(url: string | URL, init?: RequestInit): Promise<Response> {
  const u = String(url);
  if (u.startsWith("/api/prefs") && init?.method === "PUT") {
    putCalls.push({ body: JSON.parse(String(init.body)) });
    return Promise.resolve(
      new Response(JSON.stringify(fakePrefs(putResponseRanked)), { status: 200 }),
    );
  }
  throw new Error("Indexing.render.test.tsx: unexpected fetch " + u);
}

beforeEach(() => {
  putCalls = [];
  putResponseRanked = false;
  globalThis.fetch = fakeFetch as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

async function mount(prefs: Prefs, onChange: (p: Prefs) => void) {
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(RankedSearchToggle, { prefs, onChange }));
  });
  return box;
}

describe("RankedSearchToggle", () => {
  test("renders checked when the preference is ranked (the default)", async () => {
    const box = await mount(fakePrefs(true), () => {});
    const input = box.root.findByType("input");
    expect(input.props.checked).toBe(true);
    await act(async () => box.unmount());
  });

  test("renders unchecked when the persisted preference is unranked", async () => {
    const box = await mount(fakePrefs(false), () => {});
    const input = box.root.findByType("input");
    expect(input.props.checked).toBe(false);
    await act(async () => box.unmount());
  });

  test("clicking writes ranked_search_enabled=false and hands the parent the new Prefs", async () => {
    putResponseRanked = false;
    let latest: Prefs | null = null;
    const box = await mount(fakePrefs(true), (p) => {
      latest = p;
    });
    const input = box.root.findByType("input");
    await act(async () => input.props.onChange({}));
    expect(putCalls).toHaveLength(1);
    expect(putCalls[0].body).toEqual({ ranked_search_enabled: false });
    expect((latest as unknown as Prefs | null)?.indexing.ranked).toBe(false);
    await act(async () => box.unmount());
  });

  test("clicking again from unranked writes ranked_search_enabled=true", async () => {
    putResponseRanked = true;
    const box = await mount(fakePrefs(false), () => {});
    const input = box.root.findByType("input");
    await act(async () => input.props.onChange({}));
    expect(putCalls).toHaveLength(1);
    expect(putCalls[0].body).toEqual({ ranked_search_enabled: true });
    await act(async () => box.unmount());
  });

  test("the toggle publishes into the shared preference cache the search boxes read",
    async () => {
      putResponseRanked = false;
      const box = await mount(fakePrefs(true), () => {});
      const { useRankedSearchEnabled, publishRankedSearchEnabled } =
        await import("@apps/explorer/lib/ranked-search-pref");
      // Known starting state, independent of whatever an earlier test file in
      // this same bun process last published (the cache is module-level).
      publishRankedSearchEnabled(true);
      let reader!: ReactTestRenderer;
      function Reader() {
        return createElement("span", { "data-ranked": useRankedSearchEnabled() });
      }
      await act(async () => {
        reader = create(createElement(Reader));
      });
      expect(reader.root.findByType("span").props["data-ranked"]).toBe(true);

      const input = box.root.findByType("input");
      await act(async () => input.props.onChange({}));

      expect(reader.root.findByType("span").props["data-ranked"]).toBe(false);
      await act(async () => {
        box.unmount();
        reader.unmount();
      });
    });
});

// -------------------------------------------------- IndexingPanel's scan line

// The bug this covers: `files` alone is only the NEWLY-walked count
// (index/store.py's `Sink` credits an unchanged dir to `reused` instead) —
// the panel used to render `status.files` bare, so a rescan that reused most
// of a tree read as barely moving and then jumped straight to the finished
// `files_indexed` count the instant the scan completed. See DECISIONS.md,
// the Activity-card fix this panel now matches.
function fakeConfig(): IndexConfig {
  return {
    roots: ["/Users/tester"],
    configured_roots: ["/Users/tester"],
    ignore: [],
    defaults: [],
    location: "/Users/tester/.fused-render/index",
  };
}

function fakeStatus(over: Partial<IndexStatus> = {}): IndexStatus {
  return {
    scanning: true,
    has_index: true,
    files_indexed: 672424,
    last_completed_at: 1,
    running: true,
    run_id: "r1",
    root: "/Users/tester",
    phase: "",
    dirs: 0,
    files: 0,
    reused: 0,
    error: null,
    ...over,
  };
}

function panelFetch(status: IndexStatus) {
  return (url: string | URL): Promise<Response> => {
    const u = String(url);
    if (u.startsWith("/api/index/config")) {
      return Promise.resolve(new Response(JSON.stringify(fakeConfig()), { status: 200 }));
    }
    if (u.startsWith("/api/index/status")) {
      return Promise.resolve(new Response(JSON.stringify(status), { status: 200 }));
    }
    throw new Error("Indexing.render.test.tsx (panel): unexpected fetch " + u);
  };
}

function allText(node: ReactTestRendererJSON | ReactTestRendererJSON[] | string | null): string {
  if (node === null) return "";
  if (typeof node === "string") return node;
  if (Array.isArray(node)) return node.map(allText).join("");
  return allText(node.children as ReactTestRendererJSON[] | null);
}

async function mountPanel(prefs: Prefs, status: IndexStatus) {
  globalThis.fetch = panelFetch(status) as typeof fetch;
  let box!: ReactTestRenderer;
  await act(async () => {
    box = create(createElement(IndexingPanel, { prefs, onChange: () => {} }));
  });
  // Two rounds: `getIndexConfig()` and `useIndexStatus`'s own `indexStatus()`
  // call both resolve as separate microtasks after the first `act`.
  await act(async () => {});
  return box;
}

describe("IndexingPanel's scanning line", () => {
  test("adds reused to the live files count — a rescan that reuses most of a tree does not read as barely started", async () => {
    const box = await mountPanel(
      fakePrefs(true),
      fakeStatus({ scanning: true, running: true, files: 200, reused: 9800 }),
    );
    const text = allText(box.toJSON() as ReactTestRendererJSON);
    expect(text).toContain("10,000 files so far");
    expect(text).not.toContain("200 files so far");
    await act(async () => box.unmount());
  });

  test("no reused entries yet still reads correctly (files alone)", async () => {
    const box = await mountPanel(
      fakePrefs(true),
      fakeStatus({ scanning: true, running: true, files: 42, reused: 0 }),
    );
    const text = allText(box.toJSON() as ReactTestRendererJSON);
    expect(text).toContain("42 files so far");
    await act(async () => box.unmount());
  });
});
