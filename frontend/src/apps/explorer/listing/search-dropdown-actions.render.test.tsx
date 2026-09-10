// SPEC-omnibox-search-affordance.md scope item 4 (variant E), DRIVEN: the
// completion dropdown's new action row, exercised through a real render
// rather than grepped source — this is exactly the kind of sequence
// (type, debounce, arrow, Enter) that a text-parsing suite can't see. Same
// harness pattern as FileSearchField.render.test.tsx: a `fetch` stub (not
// `mock.module`, which replaces the module registry for the whole bun
// process — see that file's own header) plus the virtual Clock from
// hook-harness.ts standing in for the field's debounce timers.
//
// FileSearchField is the harness, not SearchField directly: SearchField
// alone still needs the same real `useListingSearch`/`useTypedPathAddress`/
// `useCompletion` hooks wired up by a host, and FileSearchField is the
// simpler of the two (no URL sync, no `q=` navigation seeding) while
// rendering the exact same SearchField markup this spec changed.
//
// The HARD CONSTRAINT this file exists to guard (SPEC's own name for it):
// PR #1091 fixed a HIGH-severity bug where Enter opened an arbitrary first
// row for a relative path-shaped query. Adding action rows to the dropdown
// walks back into that blast radius, so "a path-shaped query's bare Enter
// still resolves the path, never the action row" gets its own test below,
// not just an inference from the source.
import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { Clock, Deferred } from "@apps/explorer/listing/hook-harness";
import { resetFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { resetSearchSlot } from "@apps/explorer/search-slot";
import { resetHome } from "@apps/explorer/listing/home-path";

// FINDING 7 (code review, 2026-09-10), REVISED (2026-09-10, CI failure on
// paneUrl.test.ts): this file used to `mock.module("@platform/lib/router",
// ...)` with a COMPLETE replacement (every named export re-typed by hand) to
// dodge the missing-export crash the two pre-existing partial stubs
// (`useListingSearch.render.test.ts`, `useListingSelection.render.test.ts`)
// can cause elsewhere in a full run. That traded one process-wide problem
// for another: bun's `mock.module` replaces the module registry entry for
// the WHOLE PROCESS, so `paneUrl.ts`'s `withPreviewFlag` import — resolved
// by whichever test file happens to load it AFTER this one, e.g.
// `paneUrl.test.ts`, completely unrelated to this branch — silently got this
// file's `(src) => src` identity stub instead of the real implementation,
// failing `paneSrcFor`'s idempotence assertion on Linux CI (file enumeration
// order differs from macOS; `bun test`, `.github/workflows/test.yml:142`).
//
// The fix: don't touch the module registry at all. `spyOn` the ONE function
// this file needs to observe (`navigate`) directly on the real, imported
// module object, and `mockRestore()` it in `afterEach` — scoped to this
// file's own tests and reversible, so every file that loads
// `@platform/lib/router` after this one (in either direction) sees the real
// module again, `withPreviewFlag` included. This also means every OTHER
// export (`urlForFsPath`, `replaceSearch`, `currentUrl`, …) is the genuine
// implementation for this file's own tests too — they run against the real
// globalThis.history/document/window stubs set up in beforeEach below
// exactly the way production code does, rather than a hand-typed
// approximation of them.
let navigateCalls: { fsPath: string; opts: unknown }[];
let navigateSpy: ReturnType<typeof spyOn>;

const realFetch = globalThis.fetch;
let configReply: Deferred<{ home: string }>;

type Entry = { name: string; is_dir: boolean; size: number | null };
let listDirEntries: Record<string, Entry[]>;
let statOkPaths: Set<string>;

function fakeFetch(url: string | URL): Promise<Response> {
  const u = String(url);
  if (u.startsWith("/api/config")) {
    return configReply.promise.then((data) => new Response(JSON.stringify(data), { status: 200 }));
  }
  if (u.startsWith("/api/fs/list")) {
    const path = decodeURIComponent(u.split("path=")[1].split("&")[0]);
    const entries = listDirEntries[path] ?? [];
    return Promise.resolve(
      new Response(JSON.stringify({ path, entries }), { status: 200 }),
    );
  }
  if (u.startsWith("/api/fs/stat")) {
    const path = decodeURIComponent(u.split("path=")[1].split("&")[0]);
    if (statOkPaths.has(path)) {
      const name = path.split("/").pop() ?? path;
      return Promise.resolve(
        new Response(
          JSON.stringify({
            path,
            name,
            is_dir: false,
            size: 10,
            mtime: 0,
            templates: [],
          }),
          { status: 200 },
        ),
      );
    }
    return Promise.resolve(new Response(JSON.stringify({ error: "not found" }), { status: 404 }));
  }
  // Everything else (the rank request, prefs) — a 404 the hooks behind it
  // already treat as "no answer yet" rather than a crash, same as
  // FileSearchField.render.test.tsx's own default branch.
  return Promise.resolve(new Response(JSON.stringify({ error: "unexpected" }), { status: 404 }));
}

(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };

const { FileSearchField } = await import("@apps/explorer/FileSearchField");
// Imported dynamically, AFTER the `location` stub above and AFTER
// FileSearchField's own transitive import of this module — a static
// `import * as router` here would be hoisted ahead of that stub and crash
// router.ts's module-init IIFE (`location is not defined`). By this point
// the module is already evaluated and cached (FileSearchField -> SearchField
// -> router), so this just returns the same namespace object — no second
// evaluation, no ordering hazard.
const router = await import("@platform/lib/router");

const clock = new Clock();
const mounted: ReactTestRenderer[] = [];

beforeEach(() => {
  configReply = new Deferred<{ home: string }>();
  listDirEntries = {};
  statOkPaths = new Set();
  navigateCalls = [];
  blurCount = 0;
  navigateSpy = spyOn(router, "navigate").mockImplementation((fsPath: string, opts?: unknown) => {
    navigateCalls.push({ fsPath, opts });
  });
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
  navigateSpy.mockRestore();
  delete (globalThis as Record<string, unknown>).history;
  delete (globalThis as Record<string, unknown>).document;
  resetFolderChrome();
  resetSearchSlot();
  resetHome();
});

async function flush(fn: () => void = () => {}): Promise<void> {
  await act(async () => {
    fn();
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function tick(ms: number): Promise<void> {
  await act(async () => {
    clock.advance(ms);
    await Promise.resolve();
    await Promise.resolve();
  });
}

// react-test-renderer gives host nodes a null `instance` unless told
// otherwise, so `searchInputRef.current` (SearchField.tsx's own ref onto
// the real <input>) is null by default and every `?.focus()`/`?.blur()`/
// `?.select()` call on it silently no-ops — fine for tests that don't care,
// but ITEM 2's fix depends on `clearSearchQuery` actually calling
// `.blur()`, so `blurCount` gives that call somewhere to land and be
// observed.
let blurCount = 0;

function mount(fsPath: string): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(createElement(FileSearchField, { active: true, fsPath }), {
      createNodeMock: (element) =>
        element.type === "input"
          ? { focus: () => {}, select: () => {}, blur: () => { blurCount++; } }
          : null,
    });
  });
  mounted.push(renderer);
  return renderer;
}

function input(renderer: ReactTestRenderer) {
  return renderer.root.findByProps({ type: "search" });
}

// The button carries the search grammar through `data-hint` (hints.ts), not
// a native `title` — see SearchField.tsx's own comment on the button. This
// finds it by that attribute rather than by its (locale-dependent) aria-label
// text.
function searchButtons(renderer: ReactTestRenderer) {
  return renderer.root.findAll(
    (n) => n.type === "button" && typeof n.props["data-hint"] === "string",
  );
}

interface JsonNode {
  type: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  props: Record<string, any>;
  children: (JsonNode | string)[] | null;
}

function jsonNodes(renderer: ReactTestRenderer): JsonNode[] {
  const root = renderer.toJSON();
  const list = Array.isArray(root) ? root : root ? [root] : [];
  const out: JsonNode[] = [];
  const walk = (n: JsonNode | string | null): void => {
    if (n === null || typeof n === "string") return;
    out.push(n);
    for (const child of n.children ?? []) walk(child);
  };
  for (const n of list as (JsonNode | string)[]) walk(n);
  return out;
}

function completionRows(renderer: ReactTestRenderer): JsonNode[] {
  return jsonNodes(renderer).filter(
    (n) =>
      typeof n.props.className === "string" &&
      /(^|\s)listing-completion-row(\s|$)/.test(n.props.className as string),
  );
}

function rowText(row: JsonNode): string {
  const collect = (n: JsonNode | string | (JsonNode | string)[] | null): string => {
    if (n === null) return "";
    if (typeof n === "string") return n;
    if (Array.isArray(n)) return n.map(collect).join("");
    return collect(n.children);
  };
  return collect(row);
}

async function focusAndType(renderer: ReactTestRenderer, value: string): Promise<void> {
  const box = input(renderer);
  await flush(() => box.props.onFocus());
  await flush(() => box.props.onChange({ target: { value } }));
  // Past the debounce that turns the live `query` into the deferred `q`
  // useListingSearch answers `searching` off, AND past useCompletion's own
  // listDir debounce, AND past useTypedPathAddress's own statPath debounce.
  await tick(250);
  await flush();
}

describe("the dropdown's action row", () => {
  // SPEC-omnibox-search-affordance.md correction (2026-09-10, defect 1): an
  // UNGATED word or glob is already answering live in the rows below — no
  // offer, or it would read as "nothing has happened yet" over results
  // already on screen.
  test("an ungated word offers nothing — it is already searching live", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "report");

    expect(completionRows(renderer).length).toBe(0);
  });

  // FINDING 2 (code review, 2026-09-10): the row used to claim "this
  // folder" even though pressing it searches a genuinely DIFFERENT folder
  // (/mnt/other, not /home/iamsdas) — corrected to name the real folder,
  // reusing the retired banner's own wording (`folderToOpen`).
  test("a GATED non-path query (escapes to a genuinely different folder) names that folder, not 'this folder'", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    // A glob anchored somewhere other than the folder being searched
    // (parentPath is /home/iamsdas) — escapesFsPath says this one genuinely
    // escapes, so it waits for Enter rather than searching live, and the
    // offer row is what tells the user that.
    await focusAndType(renderer, "/mnt/other/*.json");

    const rows = completionRows(renderer);
    expect(rows.length).toBe(1);
    expect(rowText(rows[0])).toContain("Press Enter to open /mnt/other and search");
    expect(rowText(rows[0])).not.toContain("this folder");
    expect(rows[0].props.className).toContain("listing-completion-action");
  });

  test("a path-shaped query that does not resolve gets the not-found notice plus a search offer", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    // No entries at this dir — no folder completions to compete with the
    // notice/action pair this test is actually about.
    listDirEntries["/mnt/data"] = [];
    await focusAndType(renderer, "/mnt/data/nope");

    const rows = completionRows(renderer);
    // One notice row, one action row.
    expect(rows.length).toBe(2);
    expect(rowText(rows[0])).toContain("No such file or folder: nope");
    expect(rows[0].props.className).not.toContain("listing-completion-action");
    expect(rowText(rows[1])).toContain('Search this folder for "nope"');
  });

  // FINDING 3 (code review, 2026-09-10): the not-found report used to fire
  // for every uncommitted, half-typed path — even one with a live
  // completion sitting right below it. It now shows only when the typed
  // text resolves to nothing at all.
  test("a half-typed path WITH a live completion shows no not-found notice", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    listDirEntries["/home/iamsdas"] = [{ name: "Documents", is_dir: true, size: null }];
    await focusAndType(renderer, "/home/iamsdas/Doc");

    const rows = completionRows(renderer);
    expect(rows.some((r) => rowText(r).includes("No such file or folder"))).toBe(false);
    expect(rows.some((r) => rowText(r).includes("Search this folder for"))).toBe(false);
    expect(rows.some((r) => rowText(r).includes("Documents"))).toBe(true);
  });

  test("a half-typed path with NO completions still shows the not-found notice", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    listDirEntries["/home/iamsdas"] = [{ name: "Documents", is_dir: true, size: null }];
    await focusAndType(renderer, "/home/iamsdas/zzzz");

    const rows = completionRows(renderer);
    expect(rows.some((r) => rowText(r).includes("No such file or folder: zzzz"))).toBe(true);
  });
});

describe("ITEM 10 (running-screen review, 2026-09-10): the offer/Enter actually OPENS the folder it names", () => {
  // Bug as reported: `~/*/*.zip` committed while standing in `~/Downloads`
  // ran the search (31 matches, rows named "Downloads/canvas_1.zip") but
  // the URL/breadcrumb never moved off Downloads — commitInPlace used to
  // mean "call commitSearch()", which searches without navigating. Fixed
  // to navigate to `resolveFolderToOpen`'s folder, carrying the query
  // text unchanged via `navigate(folder, { isDir: true, q })`.
  test("pressing the action row navigates to the named folder with the query intact", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "/mnt/other/*.json");

    const rows = completionRows(renderer);
    expect(rows.length).toBe(1);
    await flush(() =>
      rows[0].props.onMouseDown({ preventDefault: () => {} }),
    );

    expect(navigateCalls.length).toBe(1);
    expect(navigateCalls[0].fsPath).toBe("/mnt/other");
    expect(navigateCalls[0].opts).toEqual({ isDir: true, q: "/mnt/other/*.json" });
  });

  test("a bare Enter on a gated query (nothing arrowed to) navigates the same way", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "/mnt/other/*.json");

    await flush(() =>
      input(renderer).props.onKeyDown({ key: "Enter", preventDefault: () => {} }),
    );

    expect(navigateCalls.length).toBe(1);
    expect(navigateCalls[0].fsPath).toBe("/mnt/other");
    expect(navigateCalls[0].opts).toEqual({ isDir: true, q: "/mnt/other/*.json" });
    // The query text in the box is untouched by the navigation itself —
    // still the full thing the user typed, not rewritten or cleared.
    expect(input(renderer).props.value).toBe("/mnt/other/*.json");
  });

  test("a `~`-rooted gated query resolves through home before navigating", async () => {
    // A `~`-rooted query genuinely escapes only when the folder open on
    // screen is NOT itself under home (a query pointing back inside the
    // box's own subtree is a live-filtering append, not an escape — see
    // escapesFsPath's own segment-comparison doc comment). Mounted at
    // /tmp/staging, not under home, so "~/other/*.py" is a real escape.
    const renderer = mount("/tmp/staging/x.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "~/other/*.py");

    await flush(() =>
      input(renderer).props.onKeyDown({ key: "Enter", preventDefault: () => {} }),
    );

    expect(navigateCalls.length).toBe(1);
    // folderToOpen("~/other/*.py") is "~/other"; resolveFolderToOpen
    // expands "~" against the resolved home before navigate() ever sees it.
    expect(navigateCalls[0].fsPath).toBe("/home/iamsdas/other");
    expect(navigateCalls[0].opts).toEqual({ isDir: true, q: "~/other/*.py" });
  });
});

describe("the hard behavioural constraint (PR #1091's HIGH-severity fix)", () => {
  test("a path-shaped query's bare Enter still resolves the path, never the action row", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    statOkPaths.add("/mnt/data/report.csv");
    listDirEntries["/mnt/data"] = [{ name: "report.csv", is_dir: false, size: 10 }];
    await focusAndType(renderer, "/mnt/data/report.csv");

    // Never arrowed — the default, resting highlight.
    const rows = completionRows(renderer);
    expect(rows.some((r) => r.props["aria-selected"] === true)).toBe(false);

    await flush(() =>
      input(renderer).props.onKeyDown({
        key: "Enter",
        preventDefault: () => {},
      }),
    );

    // Enter navigated the real path...
    expect(navigateCalls.length).toBe(1);
    expect(navigateCalls[0].fsPath).toBe("/mnt/data/report.csv");
    // ...rather than running the action row's rewrite-to-a-plain-word path,
    // which would have replaced the query with "report.csv"'s own basename
    // and left the full path behind.
    expect(input(renderer).props.value).toBe("/mnt/data/report.csv");
  });
});

describe("arrow-key navigation across the action row", () => {
  // FINDING 3 (code review, 2026-09-10) changed what this describe block can
  // demonstrate: the not-found row (notice + action) is now suppressed the
  // moment ANY real completion exists for the same text (see "the dropdown's
  // action row" above), so an action row and real folder completions no
  // longer coexist in the dropdown AT ALL — the gated/escaping branch never
  // had completions to begin with (a query containing "*" is refused
  // outright by `completion-target.ts`'s `completionTarget`), and finding 3
  // closed the one remaining case (the missing-path branch). This test now
  // covers arrow-key navigation with the action row as the dropdown's ONLY
  // row, via the gated-glob scenario.
  test("arrows to, and wraps back onto, the lone action row when it is the only row", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "/mnt/other/*.json");

    const highlightedIdx = () => {
      const hit = completionRows(renderer).find((r) => r.props.className.includes("highlight"));
      return hit ? hit.props["data-idx"] : undefined;
    };

    const indexed = completionRows(renderer).filter((r) => "data-idx" in r.props);
    expect(indexed.length).toBe(1);
    expect(highlightedIdx()).toBeUndefined();

    await flush(() =>
      input(renderer).props.onKeyDown({
        key: "ArrowDown",
        preventDefault: () => {},
        stopPropagation: () => {},
      }),
    );
    expect(highlightedIdx()).toBe(0);
    const action = completionRows(renderer).find((r) => r.props["data-idx"] === 0)!;
    expect(action.props.className).toContain("listing-completion-action");

    // Only row: wraps back onto itself rather than losing the highlight.
    await flush(() =>
      input(renderer).props.onKeyDown({
        key: "ArrowDown",
        preventDefault: () => {},
        stopPropagation: () => {},
      }),
    );
    expect(highlightedIdx()).toBe(0);
  });

  // FINDING 1 (code review, 2026-09-10): `completionKeyAction`'s Tab
  // default (index 0, nothing arrowed to) used to land on the action row
  // whenever one occupied that slot, destroying a typed path instead of
  // completing it — see completion-keys.test.ts's own
  // "Tab with nothing highlighted targets the given default index" and
  // "Tab still accepts an EXPLICITLY highlighted row 0" for the fix's
  // direct, differentiating coverage (old vs. new `tabDefaultIndex`
  // behaviour). A DRIVEN reproduction of it through this component is no
  // longer possible in this codebase: finding 3's fix above means an action
  // row and a real folder completion never appear together any more (the
  // one scenario finding 1 quoted, "/home/iamsdas/sub/nope" with
  // "nopeish.csv" in that folder, now suppresses the action row entirely —
  // see "a half-typed path WITH a live completion shows no not-found
  // notice"), so `SearchField.tsx`'s own `tabDefaultIndex` guard
  // (`hasAction && completion.items.length > 0`) is presently unreachable
  // through any live combination of props this component can produce.
  // Kept anyway per the finding's explicit "must-fix", as defensive
  // correctness against a future change that reintroduces coexistence.
});

describe("arrow keys stop at the dropdown when it is open (defect: one keypress moved two things)", () => {
  // The user's screenshot showed `Downloads.json` highlighted in the
  // dropdown while `Archive` was ALSO selected in the file listing behind
  // it — one ArrowDown reaching both useListingSelection's document-level
  // keydown listener (registered on `document`, bubble phase — see its own
  // file) and this field's own onKeyDown. react-test-renderer never
  // dispatches a real bubbling DOM event (every test in this file calls
  // `props.onKeyDown` directly), so a true end-to-end bubble-and-stop can't
  // be driven here — these tests instead verify the mechanism the fix
  // relies on directly: whether SearchField's own handler calls
  // `stopPropagation()`, which is exactly what keeps a real keydown from
  // ever reaching `document`'s bubble-phase listener.
  test("dropdown OPEN: ArrowDown stops propagation, so the listing behind it can't also move", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    // Same gated-glob scenario as the arrow-key test above: a single action
    // row, dropdown open (showCompletion true).
    await focusAndType(renderer, "/mnt/other/*.json");

    let stopped = false;
    await flush(() =>
      input(renderer).props.onKeyDown({
        key: "ArrowDown",
        preventDefault: () => {},
        stopPropagation: () => {
          stopped = true;
        },
      }),
    );
    expect(stopped).toBe(true);
  });

  test("dropdown CLOSED: ArrowDown does not stop propagation — the listing keeps navigating from the search box", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    // Never focused/typed into: showCompletion is false (fieldActive is
    // false), so completionKeyAction returns "none" for arrows and the
    // preserved behaviour (arrows drive the listing from the search box)
    // must still apply — this handler must not touch propagation at all.
    let stopped = false;
    await flush(() =>
      input(renderer).props.onKeyDown({
        key: "ArrowDown",
        preventDefault: () => {},
        stopPropagation: () => {
          stopped = true;
        },
      }),
    );
    expect(stopped).toBe(false);
  });
});

describe("the clear button exits the omnibox entirely (ITEM 2, 2026-09-10)", () => {
  // Before the fix: the clear button's own onMouseDown calls
  // preventDefault() (so the box's own text isn't lost to the browser's
  // native mousedown-blur before the click completes), which ALSO meant the
  // field never actually blurred — `clearSearchQuery` only emptied the
  // query, leaving `fieldActive` true. An empty query is pristine by
  // definition, so the teaching panel's gate (`fieldActive && pristine`)
  // was satisfied by the very click meant to leave it. Driven here through
  // the real completion dropdown (this harness's FileSearchField passes no
  // `entries`, so the teaching-examples panel itself never renders through
  // it — see search-completion-width.test.ts for that surface, and DECISIONS'
  // "Correction 4" for why `entries: []` yields no examples by design) —
  // the underlying defect is the same one either dropdown surface would
  // show: the box looks unfocused but the dropdown stays open behind it.
  test("clicking clear closes the dropdown, empties the box, and returns keyboard focus to the listing", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    listDirEntries["/home/iamsdas"] = [{ name: "Documents", is_dir: true, size: null }];
    await focusAndType(renderer, "/home/iamsdas/Doc");

    // Dropdown is genuinely open with a real completion.
    expect(completionRows(renderer).some((r) => rowText(r).includes("Documents"))).toBe(true);

    const clearButton = renderer.root.findByProps({ "aria-label": "Clear search" });
    await flush(() =>
      clearButton.props.onMouseDown({ preventDefault: () => {} }),
    );

    expect(input(renderer).props.value).toBe("");
    // The dropdown is gone, not just re-keyed to an empty query.
    expect(completionRows(renderer).length).toBe(0);
    // Back to the resting, unfocused, pre-filled-on-next-focus state: the
    // search button reoccupies the trailing slot, which only renders once
    // BOTH `pinnedOpen` and `hasClear` are false.
    expect(searchButtons(renderer).length).toBe(1);
    expect(blurCount).toBe(1);
  });
});

describe("the search button (SPEC scope item 3, words-stay revision)", () => {
  // It pins the field open through the same `requestSearchFocus` path
  // Breadcrumb.tsx's ⌘L listener uses, but seeds "" where ⌘L seeds the
  // current address: this button says "Search", and a search box that opens
  // holding the folder you are already looking at has to be cleared before
  // it can be used. This drives the button's real onClick rather than
  // asserting on source text alone.
  test("pressing it opens the field EMPTY and pins it open", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));

    const button = searchButtons(renderer)[0];
    expect(input(renderer).props.value).toBe("");

    await flush(() => button.props.onClick());

    // Nothing in it — not the crumb bar's own path, which is what ⌘L seeds.
    expect(input(renderer).props.value).toBe("");
    // The button itself only renders while unpinned — pressing it pins the
    // field open, so it should be gone from the tree now.
    expect(searchButtons(renderer).length).toBe(0);
  });
});
