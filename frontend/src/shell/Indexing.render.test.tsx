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
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { Prefs } from "@platform/lib/api";
import { RankedSearchToggle } from "@shell/Indexing";

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
