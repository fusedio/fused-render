// FINDING 1 (Cursor Bugbot, PR #1092, verified against source): the Search
// button called `requestSearchFocus("")` but the box opened holding the
// current path anyway. `requestSearchFocus` fires its listeners, and the
// ONE registered here (SearchField.tsx) does `setQuery(seed)` and then
// `searchInputRef.current?.focus()` — a real `.focus()` call fires the
// input's own `onFocus` SYNCHRONOUSLY, before React has committed the
// `setQuery` above it, so `onFocus` reads `query` from its own still-stale
// closure (whatever it was before this click — empty, since the button only
// renders at rest) and its own "empty? seed the current path" rule wrote
// the path straight back over the button's "" a moment after it landed.
//
// Same harness as search-dropdown-actions.render.test.tsx: FileSearchField
// is the concrete host (SearchField itself takes no data hooks of its own),
// a `fetch` stub stands in for the one config lookup this box makes
// (home-path.ts), and `createNodeMock` gives the `<input>` an inert
// `focus`/`select`/`blur` so this suite never depends on a real DOM
// dispatching a real focus event.
//
// That last part is exactly why this suite cannot just click the button and
// read the result: the mocked `.focus()` is a no-op, so no `onFocus` would
// ever fire on its own, and the ORIGINAL bug — onFocus racing ahead of the
// just-issued setQuery — would go untested by construction. Instead, each
// test captures the input's own `onFocus` handler from the render immediately
// BEFORE the request (the exact closure a real synchronous `.focus()` call
// would invoke, still closed over the PRE-request `query`) and invokes it
// inside the same `act()` as the request — reproducing the real ordering
// (request's setQuery, then a same-tick onFocus reading stale state) without
// needing a live DOM to produce it.
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { Clock, Deferred } from "@apps/explorer/listing/hook-harness";
import { resetFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { resetSearchSlot } from "@apps/explorer/search-slot";
import { resetHome } from "@apps/explorer/listing/home-path";
import { requestSearchFocus } from "@apps/explorer/listing/search-focus";

const realFetch = globalThis.fetch;
let configReply: Deferred<{ home: string }>;

function fakeFetch(url: string | URL): Promise<Response> {
  const u = String(url);
  if (u.startsWith("/api/config")) {
    return configReply.promise.then((data) => new Response(JSON.stringify(data), { status: 200 }));
  }
  // Everything else (listDir, stat, rank, prefs) — a 404 the hooks behind
  // it already treat as "no answer yet", same as the sibling render suites.
  return Promise.resolve(new Response(JSON.stringify({ error: "unexpected" }), { status: 404 }));
}

(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };

const { FileSearchField } = await import("@apps/explorer/FileSearchField");

const clock = new Clock();
const mounted: ReactTestRenderer[] = [];
let selectCount = 0;

beforeEach(() => {
  configReply = new Deferred<{ home: string }>();
  selectCount = 0;
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

function mount(fsPath: string): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(createElement(FileSearchField, { active: true, fsPath }), {
      createNodeMock: (element) =>
        element.type === "input"
          ? { focus: () => {}, select: () => { selectCount++; }, blur: () => {} }
          : null,
    });
  });
  mounted.push(renderer);
  return renderer;
}

function input(renderer: ReactTestRenderer) {
  return renderer.root.findByProps({ type: "search" });
}

function searchButton(renderer: ReactTestRenderer) {
  return renderer.root.findAll(
    (n) => n.type === "button" && typeof n.props["data-hint"] === "string",
  )[0];
}

describe("the Search button opens the box empty", () => {
  test("a click leaves the query empty, not re-seeded with the current path", () => {
    const renderer = mount("/home/user/Documents");
    // Captured BEFORE the click: the exact `onFocus` closure a real
    // `.focus()` call fires synchronously mid-click, still reading the
    // PRE-click `query` (empty — the button only renders while resting).
    const onFocusAtClickTime = input(renderer).props.onFocus as () => void;
    const button = searchButton(renderer);
    act(() => {
      button.props.onClick();
      onFocusAtClickTime();
    });
    expect(input(renderer).props.value).toBe("");
  });

  test("a click still pins the box open, with the placeholder as the only visible text", () => {
    const renderer = mount("/home/user/Documents");
    const onFocusAtClickTime = input(renderer).props.onFocus as () => void;
    const button = searchButton(renderer);
    act(() => {
      button.props.onClick();
      onFocusAtClickTime();
    });
    // Pinned open (so the box stays expanded rather than snapping straight
    // back to resting crumbs) and carrying no seeded text.
    expect(input(renderer).props.value).toBe("");
    expect(typeof input(renderer).props.placeholder).toBe("string");
    expect(input(renderer).props.placeholder.length).toBeGreaterThan(0);
  });
});

describe("Ctrl/Cmd+L and click-to-edit still seed the current path, selected", () => {
  test("a requestSearchFocus call carrying a path seeds it verbatim and selects it", () => {
    const renderer = mount("/home/user/Documents");
    const onFocusAtRequestTime = input(renderer).props.onFocus as () => void;
    act(() => {
      requestSearchFocus("/home/user/Documents");
      onFocusAtRequestTime();
    });
    expect(input(renderer).props.value).toBe("/home/user/Documents");
    expect(selectCount).toBeGreaterThan(0);
  });
});

describe("a plain focus not routed through requestSearchFocus is unchanged", () => {
  test("focusing the empty field directly still seeds the current path", () => {
    const renderer = mount("/home/user/Documents");
    act(() => {
      input(renderer).props.onFocus();
    });
    // No `home` has resolved in this suite (the config fetch is left
    // pending), so `contractHome` returns the path unchanged — this is the
    // existing "seed with the current address" rule, not a new behavior.
    // FileSearchField hands SearchField its PARENT folder as `fsPath` (the
    // committed query searches/navigates against the parent, not the file
    // itself) — that parent is what a plain focus seeds.
    expect(input(renderer).props.value).toBe("/home/user");
  });
});
