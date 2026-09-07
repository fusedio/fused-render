// The file explorer browses a git snapshot: `snapshotListing` (the pure
// folder-inside-vs-outside-the-app decision, apps/explorer/lib/snapshot-param.ts)
// and `useDirListing`'s own snapshot-aware fetch target.
//
// Driven through the real hook via the listing's own render harness
// (listing/hook-harness.ts — react-test-renderer, no DOM), not grepped: what
// matters is which PATH actually reaches `/api/fs/list`, not the shape of the
// source that decides it.
//
// `global.fetch` is stubbed directly for the useDirListing section, NOT
// `mock.module("@platform/lib/api", ...)`: that module is imported by dozens
// of unrelated test files sharing this process, and replacing its whole
// export surface here leaked into them (bun's mock.module is process-wide —
// see the project memory on this exact trap). Patching `fetch` and restoring
// it in `afterEach` is scoped to this file's own assertions.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { flush, renderHook } from "@apps/explorer/listing/hook-harness";
import {
  getResolvedSnapshot,
  setResolvedSnapshot,
  snapshotListing,
} from "@apps/explorer/lib/snapshot-param";

const APP = "/repo/myapp";
const DIR = "/cache/key/abc1234";
const SNAP = { sha: "abc1234", dir: DIR, app_dir: APP };

describe("snapshotListing — the pure inside/outside decision", () => {
  beforeEach(() => setResolvedSnapshot(null));

  it("with no active snapshot, lists live", () => {
    expect(snapshotListing(APP)).toEqual({ inSnapshot: false, listPath: APP });
  });

  it("a folder INSIDE the snapshotted app rewrites to the extracted tree", () => {
    setResolvedSnapshot(SNAP);
    expect(snapshotListing(APP)).toEqual({ inSnapshot: true, listPath: DIR });
    expect(snapshotListing(APP + "/sub")).toEqual({
      inSnapshot: true,
      listPath: DIR + "/sub",
    });
  });

  it("a folder OUTSIDE the snapshotted app lists live", () => {
    setResolvedSnapshot(SNAP);
    expect(snapshotListing("/repo/otherapp")).toEqual({
      inSnapshot: false,
      listPath: "/repo/otherapp",
    });
    // Same-prefix sibling, not inside the app.
    expect(snapshotListing("/repo/myapp-notes")).toEqual({
      inSnapshot: false,
      listPath: "/repo/myapp-notes",
    });
  });

  it("the way back to live: clearing the resolution clears the decision", () => {
    setResolvedSnapshot(SNAP);
    expect(snapshotListing(APP).inSnapshot).toBe(true);
    // What Listing.tsx's "Back to live" button does under the hood.
    setResolvedSnapshot(null);
    expect(getResolvedSnapshot()).toBe(null);
    expect(snapshotListing(APP)).toEqual({ inSnapshot: false, listPath: APP });
  });
});

// -------------------------------------------------- useDirListing's own fetch

const { useDirListing } = await import("@apps/explorer/listing/useDirListing");

class FakeSocket {
  onmessage: ((e: unknown) => void) | null = null;
  onclose: (() => void) | null = null;
  close() {}
}

describe("useDirListing — fetches listPath, not fsPath", () => {
  const originalFetch = globalThis.fetch;
  const originalWebSocket = (globalThis as Record<string, unknown>).WebSocket;
  const originalLocation = (globalThis as Record<string, unknown>).location;
  const originalWindow = (globalThis as Record<string, unknown>).window;
  let requested: string[] = [];

  beforeEach(() => {
    requested = [];
    (globalThis as Record<string, unknown>).WebSocket = FakeSocket;
    (globalThis as Record<string, unknown>).location = { protocol: "http:", host: "x", search: "" };
    // The "new row" tint timer reaches through `window.setTimeout` — no DOM
    // shim installs one by default here (unlike Clock.install(), unused in
    // this file since these tests need no virtual clock of their own).
    (globalThis as Record<string, unknown>).window = {
      setTimeout: (fn: () => void, ms?: number) => setTimeout(fn, ms),
      clearTimeout: (id: number) => clearTimeout(id),
    };
    globalThis.fetch = ((url: string) => {
      const path = new URL(url, "http://x").searchParams.get("path")!;
      requested.push(path);
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({ path, entries: [{ name: "a.py", is_dir: false, size: 1, mtime: 0 }] }),
      }) as unknown as Promise<Response>;
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    (globalThis as Record<string, unknown>).WebSocket = originalWebSocket;
    (globalThis as Record<string, unknown>).location = originalLocation;
    (globalThis as Record<string, unknown>).window = originalWindow;
  });

  it("with no snapshot, listPath equals fsPath and that is what is fetched", async () => {
    const box = renderHook(useDirListing, "/w/docs", "/w/docs");
    await flush();
    expect(requested).toEqual(["/w/docs"]);
    box.unmount();
  });

  it("under a snapshot, the REWRITTEN listPath is what is fetched, not fsPath", async () => {
    const box = renderHook(useDirListing, APP + "/sub", DIR + "/sub");
    await flush();
    expect(requested).toEqual([DIR + "/sub"]);
    // fsPath itself never reaches the fetch layer while a snapshot rewrite applies.
    expect(requested).not.toContain(APP + "/sub");
    box.unmount();
  });

  it("a re-render with a NEW listPath (snapshot resolved after mount) re-fetches it", async () => {
    const box = renderHook(useDirListing, APP, APP);
    await flush();
    expect(requested).toEqual([APP]);
    requested = [];
    box.rerender(APP, DIR);
    await flush();
    expect(requested).toEqual([DIR]);
    box.unmount();
  });
});
