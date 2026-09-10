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
import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { Clock, Deferred } from "@apps/explorer/listing/hook-harness";
import { resetFolderChrome } from "@apps/explorer/listing/folder-chrome";
import { resetSearchSlot } from "@apps/explorer/search-slot";
import { resetHome } from "@apps/explorer/listing/home-path";

const realFetch = globalThis.fetch;
let configReply: Deferred<{ home: string }>;
let pushStateCalls: unknown[][];

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

const clock = new Clock();
const mounted: ReactTestRenderer[] = [];

beforeEach(() => {
  configReply = new Deferred<{ home: string }>();
  listDirEntries = {};
  statOkPaths = new Set();
  pushStateCalls = [];
  resetHome();
  globalThis.fetch = fakeFetch as typeof fetch;
  clock.install();
  (globalThis as unknown as { window: Record<string, unknown> }).window.dispatchEvent = () => true;
  (globalThis as Record<string, unknown>).history = {
    state: null,
    replaceState: () => {},
    pushState: (...args: unknown[]) => {
      pushStateCalls.push(args);
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

function mount(fsPath: string): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(createElement(FileSearchField, { active: true, fsPath }));
  });
  mounted.push(renderer);
  return renderer;
}

function input(renderer: ReactTestRenderer) {
  return renderer.root.findByProps({ type: "search" });
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
  test("a bare word offers to search this folder, with nothing path-shaped about it", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    await focusAndType(renderer, "report");

    const rows = completionRows(renderer);
    expect(rows.length).toBe(1);
    expect(rowText(rows[0])).toContain('Search this folder for "report"');
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
    expect(pushStateCalls.length).toBe(1);
    // ...rather than running the action row's rewrite-to-a-plain-word path,
    // which would have replaced the query with "report.csv"'s own basename
    // and left the full path behind.
    expect(input(renderer).props.value).toBe("/mnt/data/report.csv");
  });
});

describe("arrow-key navigation across the action row", () => {
  test("reaches the action row first, then lands correctly on the folder completions after it", async () => {
    const renderer = mount("/home/iamsdas/notes.txt");
    await flush(() => configReply.resolve({ home: "/home/iamsdas" }));
    listDirEntries["/home/iamsdas/sub"] = [
      { name: "report.csv", is_dir: false, size: 10 },
      { name: "readme.md", is_dir: false, size: 20 },
    ];
    // Relative, slash-bearing, non-escaping: isPathQuery is false (chip
    // reads "Search"), so the action row appears ALONGSIDE the folder
    // completions instead of instead of them.
    await focusAndType(renderer, "sub/re");

    const highlightedIdx = () => {
      const hit = completionRows(renderer).find((r) => r.props.className.includes("highlight"));
      return hit ? hit.props["data-idx"] : undefined;
    };

    expect(completionRows(renderer).length).toBe(3);
    expect(highlightedIdx()).toBeUndefined();

    await flush(() => input(renderer).props.onKeyDown({ key: "ArrowDown", preventDefault: () => {} }));
    expect(highlightedIdx()).toBe(0);
    const action = completionRows(renderer).find((r) => r.props["data-idx"] === 0)!;
    expect(action.props.className).toContain("listing-completion-action");

    await flush(() => input(renderer).props.onKeyDown({ key: "ArrowDown", preventDefault: () => {} }));
    expect(highlightedIdx()).toBe(1);
    const first = completionRows(renderer).find((r) => r.props["data-idx"] === 1)!;
    expect(rowText(first)).toContain("report.csv");

    await flush(() => input(renderer).props.onKeyDown({ key: "ArrowDown", preventDefault: () => {} }));
    expect(highlightedIdx()).toBe(2);
    const second = completionRows(renderer).find((r) => r.props["data-idx"] === 2)!;
    expect(rowText(second)).toContain("readme.md");
  });
});
