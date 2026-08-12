// The listing prefetch cache (api.ts's `listPrefetch`) and the one thing it has
// to do besides dedupe: FORGET what it knows the moment the filesystem changes.
//
// THE BUG THIS FILE EXISTS FOR — "a drag onto a breadcrumb copies the file".
// It never copied anything; the move was a real rename both times it was
// looked at. What the user saw was the SOURCE listing repainting from this
// cache after the move:
//
//   1. in /a/b/c, drag a file and hold the /a crumb — the spring-load navigates
//      to /a, which fills this cache with /a's listing AND unmounts the source
//      listing (killing its dir-watch, so nothing will ever tell /a/b/c that
//      the file left);
//   2. drop into /a/d — a real rename; the TARGET listing refetches with
//      refresh > 0, which bypasses the cache, so the destination is correct;
//   3. navigate back to /a/b/c within the 5s TTL — a FRESH MOUNT, so
//      useDirListing reads through this cache (refresh === 0) and paints the
//      PRE-MOVE listing. One file, visible in two folders, which reads as a
//      copy. It healed itself after 5s, hence "intermittent".
//
// So: any successful fs mutation must invalidate the whole map. These tests
// pin that, and the dedupe it must not lose.
import { expect, test } from "bun:test";

// api.ts is fetch-only (no router, no DOM), but the module is imported
// dynamically anyway so the fetch stub below is installed FIRST — a
// module-scope fetch capture in anything api.ts pulls in would otherwise
// snapshot the real one.
type Call = { url: string; method: string };
const calls: Call[] = [];
let listBody: unknown = { entries: [] };
let mutationStatus = 200;

(globalThis as { fetch?: unknown }).fetch = ((url: string, init?: { method?: string }) => {
  calls.push({ url, method: init?.method ?? "GET" });
  const isMutation = (init?.method ?? "GET") !== "GET";
  const status = isMutation ? mutationStatus : 200;
  return Promise.resolve({
    ok: status < 400,
    status,
    json: () =>
      Promise.resolve(
        isMutation ? (status < 400 ? { path: "/x" } : { error: "conflict" }) : listBody,
      ),
  });
}) as unknown as typeof fetch;

const api = await import("@platform/lib/api");

const listCalls = () => calls.filter((c) => c.url.startsWith("/api/fs/list")).length;

test("a second prefetch of the same dir inside the TTL reuses the first request", () => {
  // The reason the cache exists at all: the loading scaffold's Listing and the
  // real preview's Listing both ask for the same first page.
  const before = listCalls();
  const a = api.prefetchListDir("/dedupe");
  const b = api.prefetchListDir("/dedupe");
  expect(a).toBe(b);
  expect(listCalls() - before).toBe(1);
});

test("a rename invalidates the cached listing of every directory", async () => {
  // The reported bug, at its cheapest honest scale: a directory whose listing
  // was cached before a move must not be answered from that cache afterwards.
  const first = api.prefetchListDir("/a/b/c");
  await first;
  await api.renameEntry("/a/b/c/report.csv", "/a/d/report.csv");
  const after = api.prefetchListDir("/a/b/c");
  expect(after).not.toBe(first);
});

test("every fs mutation invalidates it, not just rename", async () => {
  // A rename touches two directories, a recursive delete a whole subtree, and a
  // compress writes a sibling — path arithmetic over that buys nothing, so the
  // whole map goes. Each wrapper is checked so a new one can't quietly skip it.
  const mutations: [string, () => Promise<unknown>][] = [
    ["writeFile", () => api.writeFile("/m/new.txt")],
    ["mkdir", () => api.mkdir("/m/dir")],
    ["deleteEntry", () => api.deleteEntry("/m/gone.txt")],
    ["renameEntry", () => api.renameEntry("/m/a.txt", "/m/b.txt")],
    ["copyEntry", () => api.copyEntry("/m/a.txt", "/m/c.txt")],
    ["compressEntry", () => api.compressEntry("/m/dir", "zip", "/m/dir.zip")],
  ];
  for (const [name, run] of mutations) {
    const before = api.prefetchListDir("/m");
    await before;
    await run();
    expect(api.prefetchListDir("/m"), name).not.toBe(before);
  }
});

test("a REFUSED mutation keeps the cache — nothing changed on disk", async () => {
  // Invalidating on failure would drop a listing that is still perfectly
  // accurate, and a 409 on a paste is a routine outcome (the dedupe path).
  const first = api.prefetchListDir("/refused");
  await first;
  mutationStatus = 409;
  await api.renameEntry("/refused/a.txt", "/refused/b.txt").catch(() => {});
  mutationStatus = 200;
  expect(api.prefetchListDir("/refused")).toBe(first);
});

test("clearListPrefetch invalidates for a mutation this app did not perform", () => {
  // The export exists for the dir-watch socket (listing/useDirListing), which is
  // how the shell hears about a change it did not make: a view's own
  // fused.writeFile() through /api/run, an external editor, a git checkout. Those
  // never reach the wrappers above, and before this the folder refreshed while a
  // fresh mount inside the TTL still painted the pre-change listing.
  const first = api.prefetchListDir("/manual");
  api.clearListPrefetch();
  expect(api.prefetchListDir("/manual")).not.toBe(first);
});
