// Enter, driven through the real handler.
//
// The rule under test is the one that decides whether the box may act on rows
// the user cannot have meant: with no selection, Enter opens the FIRST row,
// and while the rendered rows answer a query the user has already typed past
// that row is the previous query's top hit. The listing never blanks the list,
// so those rows are on screen by design — which is exactly why the guess has
// to be gated.
//
// Driven rather than grepped: the previous version of this test asserted that
// the handler's source contained the word `rowsAnswerQuery`, which passes for
// an inverted condition and fails for a rename.
import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test";
import { Clock, flush, renderHook } from "@apps/explorer/listing/hook-harness";
import type { RowCtx } from "@apps/explorer/listing/types";

const navigated: string[] = [];
// `navHintQCommitted` included even though this file never mounts
// `useListingSearch`: `mock.module` replaces the module process-wide (bun
// runs every test file in one process), so a mock here missing an export
// another file's `mock.module("@platform/lib/router", …)` DOES provide can
// still break that file if this one's mock wins the race — see
// useListingSearch.render.test.ts's own comment on this same export.
mock.module("@platform/lib/router", () => ({
  navigate: (p: string) => void navigated.push(p),
  replaceSearch: () => {},
  navHintQCommitted: () => false,
}));
mock.module("@platform/lib/ui-overlay", () => ({ isOverlayOpen: () => false }));

const { useListingSelection } = await import("@apps/explorer/listing/useListingSelection");

const clock = new Clock();
let listeners: ((e: unknown) => void)[] = [];

function installDocument() {
  listeners = [];
  (globalThis as Record<string, unknown>).document = {
    activeElement: null,
    // The scroll-into-view effect looks for the lead row; there is no DOM here
    // and none of these tests is about scrolling.
    querySelector: () => null,
    addEventListener: (type: string, fn: (e: unknown) => void) => {
      if (type === "keydown") listeners.push(fn);
    },
    removeEventListener: (type: string, fn: (e: unknown) => void) => {
      listeners = listeners.filter((l) => l !== fn);
    },
  };
}

/** Press a key at the document, the way the real listener receives it. */
function press(key: string, defaultPrevented = false): void {
  const e = { key, isComposing: false, defaultPrevented, shiftKey: false,
              ctrlKey: false, metaKey: false, altKey: false,
              preventDefault() { (this as { defaultPrevented: boolean }).defaultPrevented = true; } };
  for (const fn of [...listeners]) fn(e);
}

// A folder per test: the selection is remembered per folder, so sharing one
// would let an arrow-key choice in one test arm Enter in the next.
let folder = 0;
function mount(rowsAnswerQuery: boolean) {
  const dir = "/d" + folder++;
  const rows = [dir + "/README.md", dir + "/notes.md"];
  const ctx = new Map<string, RowCtx>(
    rows.map((p) => [p, { path: p, name: p.split("/").pop()!, isDir: false, parentDir: dir }]),
  );
  const box = renderHook(
    (answers: boolean) =>
      useListingSelection({
        fsPath: dir,
        navRows: rows,
        listingLoaded: true,
        rowsAnswerQuery: answers,
        searchInputRef: { current: null },
        rowCtxByPathRef: { current: ctx },
        overlayOpenRef: { current: false },
      }),
    rowsAnswerQuery,
  );
  return { ...box, top: rows[0] };
}

beforeEach(() => {
  navigated.length = 0;
  clock.install();
  installDocument();
});
afterEach(() => {
  clock.restore();
  delete (globalThis as Record<string, unknown>).document;
});

describe("Enter with nothing selected", () => {
  test("opens the top row when the rows answer the query", async () => {
    const box = mount(true);
    await flush(() => press("Enter"));
    expect(navigated).toEqual([box.top]);
    box.unmount();
  });

  test("opens NOTHING while the rows answer an older query", async () => {
    // Type "read", get README.md; type "me", and README.md is still the only
    // thing on screen. Enter must not open it — the user is mid-word.
    const box = mount(false);
    await flush(() => press("Enter"));
    expect(navigated).toEqual([]);
    box.unmount();
  });

  test("still opens a row the user actually chose", async () => {
    // Their explicit choice is not a guess: they pointed at a row they can see,
    // so it opens whatever the rows currently answer.
    const box = mount(false);
    await flush(() => press("ArrowDown")); // selects the first row
    await flush(() => press("Enter"));
    expect(navigated).toEqual([box.top]);
    box.unmount();
  });

  test("resumes opening the top row once the answer catches up", async () => {
    const box = mount(false);
    await flush(() => press("Enter"));
    expect(navigated).toEqual([]);
    box.rerender(true);
    await flush(() => press("Enter"));
    expect(navigated).toEqual([box.top]);
    box.unmount();
  });
});

describe("Enter on the zero-match glob-broadening offer", () => {
  // No navRows at all: the settled-zero-hits state this offer renders in
  // (Listing.tsx) never has real rows on screen. The offer is passed in
  // through `zeroMatchOffer`, not folded into `navRows` — this exercises the
  // one added branch inside the pre-existing `!rows.length` guard.
  function mountOffer() {
    const dir = "/d" + folder++;
    const activated: string[] = [];
    const box = renderHook(() =>
      useListingSelection({
        fsPath: dir,
        navRows: [],
        listingLoaded: true,
        rowsAnswerQuery: true,
        searchInputRef: { current: null },
        rowCtxByPathRef: { current: new Map() },
        overlayOpenRef: { current: false },
        zeroMatchOffer: { path: "\0zero-match-broaden-offer", onActivate: () => activated.push("rerun") },
      }),
    );
    return { box, activated };
  }

  test("runs the offer's callback instead of navigating anywhere", async () => {
    const { box, activated } = mountOffer();
    await flush(() => press("Enter"));
    expect(activated).toEqual(["rerun"]);
    expect(navigated).toEqual([]);
    box.unmount();
  });

  test("does nothing when there is no offer (plain empty rows)", async () => {
    const dir = "/d" + folder++;
    const box = renderHook(() =>
      useListingSelection({
        fsPath: dir,
        navRows: [],
        listingLoaded: true,
        rowsAnswerQuery: true,
        searchInputRef: { current: null },
        rowCtxByPathRef: { current: new Map() },
        overlayOpenRef: { current: false },
      }),
    );
    await flush(() => press("Enter"));
    expect(navigated).toEqual([]);
    box.unmount();
  });
});

describe("Escape", () => {
  test("clears the selection even when a clipboard op would have been pending", async () => {
    // A pending copy/cut no longer has a say here: nothing outside this hook
    // calls preventDefault() on Escape any more, but a stray `true` reaching
    // this handler (however it got set) must not stop the selection from
    // clearing either — there is no longer a second consumer of the key for
    // this branch to defer to.
    //
    // searchInputRef is a distinct sentinel, not null: with it null,
    // `document.activeElement` (also null, per this harness's installDocument)
    // would satisfy `el === searchInputRef.current` and read as "focused in
    // the search box" by coincidence rather than by fact.
    const dir = "/d" + folder++;
    const rows = [dir + "/README.md", dir + "/notes.md"];
    const ctx = new Map<string, RowCtx>(
      rows.map((p) => [p, { path: p, name: p.split("/").pop()!, isDir: false, parentDir: dir }]),
    );
    const box = renderHook(() =>
      useListingSelection({
        fsPath: dir,
        navRows: rows,
        listingLoaded: true,
        rowsAnswerQuery: true,
        searchInputRef: { current: {} as HTMLInputElement },
        rowCtxByPathRef: { current: ctx },
        overlayOpenRef: { current: false },
      }),
    );
    await flush(() => press("ArrowDown")); // selects the first row
    expect(box.current().sel.paths).toEqual([rows[0]]);
    await flush(() => press("Escape", true));
    expect(box.current().sel.paths).toEqual([]);
    box.unmount();
  });
});
