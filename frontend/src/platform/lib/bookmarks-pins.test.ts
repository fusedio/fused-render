// Pinned bookmarks (bookmarks.ts): the pinned head of the top-level array,
// the pure readers over it, and the mutators that keep it the head.
//
// A fetch stub, not `mock.module("@platform/lib/api")` — a module mock is
// process-wide in bun and breaks unrelated suites (FilesHome.render.test.tsx
// has the full story). GET /api/bookmarks serves `server`; PUT replaces it.
import { afterAll, beforeAll, beforeEach, describe, expect, test } from "bun:test";
import {
  addBookmark,
  hydrateBookmarks,
  loadBookmarks,
  moveItem,
  pinBookmark,
  pinnedBookmarks,
  refreshBookmarks,
  unpinBookmark,
  unpinnedItems,
  type BookmarkItem,
} from "@platform/lib/bookmarks";

let server: BookmarkItem[] = [];
let puts = 0;
const realFetch = globalThis.fetch;

function fakeFetch(url: string | URL, init?: RequestInit): Promise<Response> {
  if (String(url) !== "/api/bookmarks") throw new Error(`unexpected fetch ${String(url)}`);
  if (init?.method === "PUT") {
    puts++;
    server = JSON.parse(String(init.body));
    return Promise.resolve(new Response("{}", { status: 200 }));
  }
  const body = JSON.stringify({ exists: true, bookmarks: server, missing: [] });
  return Promise.resolve(new Response(body, { status: 200 }));
}

const bm = (id: string, pinned = false): BookmarkItem => ({
  id,
  name: id,
  url: `/explorer/view/${id}`,
  created_at: 0,
  ...(pinned ? { pinned: true } : {}),
});

const ids = (items: BookmarkItem[]): string[] => items.map((it) => it.id);

// Seed the server tree and pull it into the cache (hydrate is once-only, so
// every later seed goes through the poll path).
async function seed(items: BookmarkItem[]): Promise<void> {
  server = JSON.parse(JSON.stringify(items));
  await refreshBookmarks();
  puts = 0;
}

beforeAll(async () => {
  globalThis.fetch = fakeFetch as typeof fetch;
  await hydrateBookmarks();
});

afterAll(() => {
  globalThis.fetch = realFetch;
});

beforeEach(() => {
  puts = 0;
});

describe("pinnedBookmarks / unpinnedItems", () => {
  test("split the top level by flag, each in array order", () => {
    const folder: BookmarkItem = {
      id: "f",
      type: "folder",
      name: "f",
      collapsed: false,
      children: [bm("nested", true)],
    };
    const items = [bm("p1", true), bm("p2", true), bm("a"), folder, bm("b")];
    expect(ids(pinnedBookmarks(items))).toEqual(["p1", "p2"]);
    expect(ids(unpinnedItems(items))).toEqual(["a", "f", "b"]);
  });

  test("a pin out of place still reads as a pin, once", () => {
    const items = [bm("p1", true), bm("a"), bm("p2", true)];
    expect(ids(pinnedBookmarks(items))).toEqual(["p1", "p2"]);
    expect(ids(unpinnedItems(items))).toEqual(["a"]);
  });
});

describe("mutators", () => {
  test("addBookmark lands right after the pinned head", async () => {
    await seed([bm("p1", true), bm("p2", true), bm("a")]);
    await addBookmark("new", "/explorer/view/new");
    const names = loadBookmarks().map((it) => it.name);
    expect(names).toEqual(["p1", "p2", "new", "a"]);
  });

  test("addBookmark with no pins still opens the list", async () => {
    await seed([bm("a"), bm("b")]);
    await addBookmark("new", "/explorer/view/new");
    expect(loadBookmarks().map((it) => it.name)).toEqual(["new", "a", "b"]);
  });

  test("pin moves the row to the end of the pinned head", async () => {
    await seed([bm("p1", true), bm("a"), bm("b"), bm("c")]);
    await pinBookmark("c");
    expect(ids(loadBookmarks())).toEqual(["p1", "c", "a", "b"]);
    expect(ids(pinnedBookmarks(loadBookmarks()))).toEqual(["p1", "c"]);
  });

  test("unpin moves the row to the top of the unpinned list", async () => {
    await seed([bm("p1", true), bm("p2", true), bm("p3", true), bm("a")]);
    await unpinBookmark("p1");
    const items = loadBookmarks();
    expect(ids(items)).toEqual(["p2", "p3", "p1", "a"]);
    expect("pinned" in items[2]).toBe(false);
  });

  test("a nested bookmark cannot pin — no write", async () => {
    await seed([
      bm("a"),
      { id: "f", type: "folder", name: "f", collapsed: false, children: [bm("nested"), bm("x")] },
    ]);
    await pinBookmark("nested");
    expect(puts).toBe(0);
    expect(pinnedBookmarks(loadBookmarks())).toEqual([]);
  });

  test("moveItem clamps a top-level drop into the pinned head", async () => {
    await seed([bm("p1", true), bm("p2", true), bm("a"), bm("b")]);
    await moveItem("b", null, 0);
    expect(ids(loadBookmarks())).toEqual(["p1", "p2", "b", "a"]);
  });

  test("moveItem refuses to move a pinned row", async () => {
    await seed([bm("p1", true), bm("a")]);
    await moveItem("p1", null, 1);
    expect(puts).toBe(0);
    expect(ids(loadBookmarks())).toEqual(["p1", "a"]);
  });
});
