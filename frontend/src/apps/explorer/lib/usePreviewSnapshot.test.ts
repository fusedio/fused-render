// Regression coverage for code review round 3, finding 8: Preview.tsx's
// resolve effect used to just `return` on a non-404 failure, leaving the
// pane's `pending` state true forever with nothing telling a caller "this
// will never resolve without help" — a blank pane with no retry and no way
// back to live, surviving a reload since the sha stays on the URL. Mirrors
// shell/useAppPageSnapshot.test.ts's own coverage of the identical fix there
// (`error`/`retry`).
//
// Driven through the real hook via the listing's own render harness
// (hook-harness.ts — react-test-renderer, no DOM), the same pattern
// useSnapshotForFolder.test.ts already uses for the explorer's sibling hook
// — `usePreviewSnapshot` has no `apps/explorer/listing`-specific dependency,
// it just reuses that harness rather than inventing a second one.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { flush, renderHook } = await import("@apps/explorer/listing/hook-harness");
const { setResolvedSnapshot } = await import("@platform/lib/snapshot-param");
const { usePreviewSnapshot } = await import("./usePreviewSnapshot");

type FakeLocation = { search: string; pathname: string };
const curLoc = () => (globalThis as unknown as { location: FakeLocation }).location;

describe("usePreviewSnapshot", () => {
  const originalFetch = globalThis.fetch;
  const originalLocation = (globalThis as Record<string, unknown>).location;
  const originalHistory = (globalThis as Record<string, unknown>).history;
  const originalDocument = (globalThis as Record<string, unknown>).document;
  let requested: string[] = [];
  let failMode: "none" | "404" | "network" = "none";

  beforeEach(() => {
    setResolvedSnapshot(null);
    requested = [];
    failMode = "none";
    const fresh: FakeLocation = { search: "", pathname: "/w/myapp/x.py" };
    (globalThis as Record<string, unknown>).location = fresh;
    (globalThis as Record<string, unknown>).history = {
      state: null,
      replaceState: (_state: unknown, _title: string, url: string) => {
        const target = curLoc();
        const qIndex = url.indexOf("?");
        target.pathname = qIndex === -1 ? url : url.slice(0, qIndex);
        target.search = qIndex === -1 ? "" : url.slice(qIndex);
      },
    };
    (globalThis as Record<string, unknown>).document = { querySelector: () => null };
    globalThis.fetch = ((url: string) => {
      const u = new URL(url, "http://x");
      const path = u.searchParams.get("path") ?? "";
      const sha = u.searchParams.get("sha") ?? "";
      requested.push(`${path}@${sha}`);
      if (failMode === "network") {
        return Promise.reject(new TypeError("network error"));
      }
      if (sha === "deadbee0" || failMode === "404") {
        return Promise.resolve({
          ok: false,
          status: 404,
          json: () => Promise.resolve({ ok: false, error: "no app folder encloses it" }),
        }) as unknown as Promise<Response>;
      }
      return Promise.resolve({
        ok: true,
        json: () =>
          Promise.resolve({ ok: true, dir: `/cache/key/${sha}`, app_dir: "/w/myapp", entry: null }),
      }) as unknown as Promise<Response>;
    }) as typeof fetch;
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    (globalThis as Record<string, unknown>).location = originalLocation;
    (globalThis as Record<string, unknown>).history = originalHistory;
    (globalThis as Record<string, unknown>).document = originalDocument;
  });

  it("resolves _snapshot off the URL on mount", async () => {
    curLoc().search = "?_snapshot=abc1234";
    const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
    await flush();
    expect(box.current().snap).toEqual({
      sha: "abc1234",
      dir: "/cache/key/abc1234",
      app_dir: "/w/myapp",
    });
    expect(box.current().pending).toBe(false);
    expect(box.current().error).toBe(false);
    box.unmount();
  });

  it("is pending (not error) while a resolve is in flight", () => {
    curLoc().search = "?_snapshot=abc1234";
    const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
    expect(box.current().pending).toBe(true);
    expect(box.current().error).toBe(false);
    box.unmount();
  });

  it(
    "FINDING 8: a non-404 resolve failure sets error, and leaves the pane " +
      "pending (not a false 'live') rather than stuck with no signal at all",
    async () => {
      failMode = "network";
      curLoc().search = "?_snapshot=aaaa111";
      const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
      for (let i = 0; i < 8; i++) {
        await flush();
      }
      expect(box.current().error).toBe(true);
      expect(box.current().pending).toBe(true);
      expect(box.current().snap).toBe(null);
      // Still on the URL: a transient failure must not silently fall back
      // to live the way a confirmed 404 does.
      expect(curLoc().search).toBe("?_snapshot=aaaa111");
      box.unmount();
    }
  );

  it(
    "FINDING 8: retry() re-attempts the SAME sha and a subsequent success " +
      "clears the error",
    async () => {
      failMode = "network";
      curLoc().search = "?_snapshot=aaaa111";
      const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
      for (let i = 0; i < 8; i++) {
        await flush();
      }
      expect(box.current().error).toBe(true);
      requested = [];
      failMode = "none";

      await flush(() => box.current().retry());
      for (let i = 0; i < 8 && box.current().error; i++) {
        await flush();
      }

      expect(requested).toEqual(["/w/myapp/x.py@aaaa111"]);
      expect(box.current().error).toBe(false);
      expect(box.current().snap).toEqual({
        sha: "aaaa111",
        dir: "/cache/key/aaaa111",
        app_dir: "/w/myapp",
      });
      box.unmount();
    }
  );

  it("retry() is a no-op with no sha on the URL", async () => {
    const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
    await flush(() => box.current().retry());
    expect(requested).toEqual([]);
    expect(box.current().error).toBe(false);
    box.unmount();
  });

  it(
    "backToLive clears the resolution AND tells the git sidebar " +
      "(clearShellSnapshot — findings 2/7)",
    async () => {
      curLoc().search = "?_snapshot=abc1234";
      let notified = false;
      (globalThis as Record<string, unknown>).document = {
        querySelector: (sel: string) =>
          sel === ".preview-side-frame"
            ? { contentWindow: { _fusedSnapshotCleared: () => (notified = true) } }
            : null,
      };
      const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
      await flush();
      expect(box.current().snap?.sha).toBe("abc1234");

      await flush(() => box.current().backToLive());

      expect(box.current().snap).toBe(null);
      expect(box.current().sha).toBe(null);
      expect(notified).toBe(true);
      box.unmount();
    }
  );

  it(
    "ROUND 4: a URL that drops _snapshot outright clears the shared " +
      "singleton AND tells the git sidebar, not just this pane's own sha",
    async () => {
      // Regression for round 4's inventory: the `!isSha(raw)` branch used
      // to only clear THIS pane's own `snapshotSha`, leaving
      // `resolvedSnapshotState` — and the shared singleton behind it —
      // holding a stale `ResolvedSnapshot` after the pane had already gone
      // visually live, with the sidebar's Checkout still armed against it.
      const { getResolvedSnapshot } = await import("@platform/lib/snapshot-param");
      curLoc().search = "?_snapshot=abc1234";
      let notified = false;
      (globalThis as Record<string, unknown>).document = {
        querySelector: (sel: string) =>
          sel === ".preview-side-frame"
            ? { contentWindow: { _fusedSnapshotCleared: () => (notified = true) } }
            : null,
      };
      const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
      await flush();
      expect(box.current().snap?.sha).toBe("abc1234");
      expect(getResolvedSnapshot()?.sha).toBe("abc1234");

      // Stand in for browser back/forward: the URL loses `_snapshot`
      // entirely, with no `backToLive()` call from this pane involved.
      curLoc().search = "";
      box.rerender("/w/myapp/x.py", 1);
      await flush();

      expect(box.current().snap).toBe(null);
      expect(box.current().sha).toBe(null);
      expect(getResolvedSnapshot()).toBe(null);
      expect(notified).toBe(true);
      box.unmount();
    }
  );

  it(
    "a stale error from a DIFFERENT sha does not survive picking an " +
      "already-resolved one",
    async () => {
      // The already-resolved skip-check branch must clear a stale `error`
      // too — an app-page round-3 finding this hook mirrors on purpose.
      curLoc().search = "?_snapshot=abc1234";
      const box = renderHook(usePreviewSnapshot, "/w/myapp/x.py", 0);
      await flush();
      expect(box.current().snap?.sha).toBe("abc1234");

      failMode = "network";
      curLoc().search = "?_snapshot=aaaa111";
      box.rerender("/w/myapp/x.py", 1);
      for (let i = 0; i < 8; i++) {
        await flush();
      }
      expect(box.current().error).toBe(true);

      // Back to the already-resolved sha.
      curLoc().search = "?_snapshot=abc1234";
      box.rerender("/w/myapp/x.py", 2);
      await flush();

      expect(box.current().error).toBe(false);
      expect(box.current().snap?.sha).toBe("abc1234");
      box.unmount();
    }
  );
});
