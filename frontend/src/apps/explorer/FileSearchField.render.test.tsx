// FileSearchField's resting crumbs, DRIVEN: `home` arrives from `useHome`'s
// `/api/config` fetch (home-path.ts, cached across every host that calls
// it), so a mount starts with it `undefined` and only later carries the
// real value. This exercises both sides of that timing and the two shapes
// `PathCrumbs` renders once the value lands: a file under home, and a file
// outside it.
//
// A fetch stub on `globalThis.fetch`, not `mock.module("@platform/lib/api")`
// — that replaces a real ES module namespace (frozen bindings) for the whole
// bun process, not just this file, which is exactly what FilesHome.render.
// test.tsx's own header comment documents breaking CI. `getConfig()` is a
// thin `getJson` wrapper over `fetch`, so stubbing the global gets the same
// control without touching the module registry.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { Clock, Deferred } from "@apps/explorer/listing/hook-harness";
import { resetFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { resetSearchSlot } from "@apps/explorer/search-slot";
import { resetHome } from "@apps/explorer/listing/home-path";

const realFetch = globalThis.fetch;

let configReply: Deferred<{ home: string }>;

function fakeFetch(url: string | URL): Promise<Response> {
  const u = String(url);
  if (u.startsWith("/api/config")) {
    return configReply.promise.then(
      (data) => new Response(JSON.stringify(data), { status: 200 }),
    );
  }
  // Every other lookup this box could make (statPath, listDir, getPrefs,
  // indexRank) is gated on a non-empty query — the tests below never type
  // into the field, so none of these should ever fire. Answering with a 404
  // rather than throwing keeps a stray call from crashing the render (the
  // hooks behind each of those already treat a rejection as "no answer
  // yet"); the assertions below are what actually catch one happening.
  return Promise.resolve(new Response(JSON.stringify({ error: "unexpected" }), { status: 404 }));
}

(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };

const { FileSearchField } = await import("@apps/explorer/FileSearchField");

const clock = new Clock();
const mounted: ReactTestRenderer[] = [];

beforeEach(() => {
  configReply = new Deferred<{ home: string }>();
  resetHome();
  globalThis.fetch = fakeFetch as typeof fetch;
  clock.install();
  (globalThis as unknown as { window: Record<string, unknown> }).window.dispatchEvent = () => true;
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
  resetFolderChrome();
  resetSearchSlot();
  resetHome();
});

/** Every `path-crumb`/`path-crumb-sep` text node in render order, read off
 * the tree rather than grepped from a snapshot string — a `.last` crumb and
 * an ancestor link render as different element types, and this walks both
 * alike. */
function crumbTexts(renderer: ReactTestRenderer): string[] {
  const json = renderer.toJSON();
  const nodes = Array.isArray(json) ? json : json ? [json] : [];
  const out: string[] = [];
  const isCrumb = (className: unknown) =>
    typeof className === "string" &&
    /(^|\s)path-crumb(-sep)?(\s|$)/.test(className);
  const walk = (n: unknown): void => {
    if (n === null || typeof n !== "object") return;
    const el = n as { type?: string; props?: Record<string, unknown>; children?: unknown[] };
    if (isCrumb(el.props?.className)) {
      const text = (el.children ?? []).filter((c) => typeof c === "string").join("");
      out.push(text);
    }
    for (const child of el.children ?? []) walk(child);
  };
  for (const n of nodes) walk(n);
  return out;
}

async function flush(fn: () => void = () => {}): Promise<void> {
  await act(async () => {
    fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

function mount(fsPath: string): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(createElement(FileSearchField, { active: true, fsPath }));
  });
  mounted.push(renderer);
  return renderer;
}

describe("FileSearchField's resting crumbs", () => {
  test("a file under home contracts the crumb root to \"~\" once home resolves", async () => {
    const renderer = mount("/home/iamsdas/Downloads/585729635325575167.parquet.json");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    expect(crumbTexts(renderer)).toEqual([
      "~",
      "/",
      "Downloads",
      "/",
      "585729635325575167.parquet.json",
    ]);
  });

  test("a file outside home stays absolute once home resolves", async () => {
    const renderer = mount("/mnt/data/report.json");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    expect(crumbTexts(renderer)).toEqual(["/", "mnt", "/", "data", "/", "report.json"]);
  });

  test("a second mount reuses the already-resolved home with no further fetch", async () => {
    const first = mount("/home/iamsdas/Downloads/585729635325575167.parquet.json");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    expect(crumbTexts(first)).toEqual([
      "~",
      "/",
      "Downloads",
      "/",
      "585729635325575167.parquet.json",
    ]);
    act(() => first.unmount());
    mounted.pop();

    // A fresh box, same window, no further answer queued on `configReply` —
    // if this box made its own independent `/api/config` request it would
    // have nothing to resolve it and would render the raw path forever.
    // Reading the tree right after `mount`, with no `flush` in between,
    // catches the very first render: the shared cache already has `home`
    // resolved by the mount above, so there is no second round trip to wait
    // out at all.
    const second = mount("/home/iamsdas/Documents/report.txt");
    expect(crumbTexts(second)).toEqual(["~", "/", "Documents", "/", "report.txt"]);
  });

  test("before the config fetch answers, the crumbs render the raw path rather than nothing", async () => {
    const renderer = mount("/home/iamsdas/Downloads/585729635325575167.parquet.json");
    // No flush past `configReply` — `home` is still `undefined`, exactly the
    // window between mount and the fetch landing.
    await flush();
    expect(crumbTexts(renderer)).toEqual([
      "/",
      "home",
      "/",
      "iamsdas",
      "/",
      "Downloads",
      "/",
      "585729635325575167.parquet.json",
    ]);
  });
});
