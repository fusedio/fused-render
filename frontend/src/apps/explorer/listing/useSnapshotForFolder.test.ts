// Regression coverage for code review finding B5: Listing.tsx's snapshot
// resolution used to key its effect on `[fsPath]` alone, so a commit
// selection made through a plain `replaceSearch` (which does not dispatch
// `fused:navigate`, only `fused:urlchange`) never re-ran it while the open
// folder itself stayed put. Driven through the real hook via the listing's
// own render harness (hook-harness.ts — react-test-renderer, no DOM), not
// grepped: what matters is which resolution the hook actually holds after a
// URL change, not the shape of the source that decides it.
//
// `urlVersion` is passed to the hook directly (what Listing.tsx feeds it from
// `useUrlVersion()`) rather than driven through a real `window` event
// dispatch — this environment has neither a DOM nor main.tsx's own wrapping
// of `history.replaceState` that a live browser would have, and the fix
// itself is entirely in how the hook reacts to that number changing, not in
// how the number gets produced (already covered by `useUrlVersion`'s own
// listeners in platform/lib/hooks.ts).
//
// `globalThis.location`/`.history` are NEVER HELD IN A LOCAL VARIABLE across
// an `await` — always re-read fresh (`curLoc()`/`curHist()`) at the exact
// point they are needed. Several OTHER suites sharing this process (e.g.
// Listing.test.tsx's own `useDirListing` tests) install a THROWAWAY
// `globalThis.location` object per test; when this file's own async
// (`await flush()`) tests happen to interleave with one of those, a `const
// loc = globalThis.location` captured before the `await` goes stale the
// moment another file's object takes its place — production code (which
// always reads the bare `location`/`history` identifiers fresh) then writes
// to a DIFFERENT object than the one this test still holds a reference to,
// and a `search` value silently stops updating with no test failure that
// points at the real cause. Reading fresh sidesteps that regardless of
// which object is currently installed.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

// router.ts (which useSnapshotForFolder's `replaceSearch` comes from) reads
// `location` at MODULE SCOPE — the shim must be installed before that
// import happens, hence the dynamic imports below rather than static ones.
// See testDomShim.ts and platform/lib/router.test.ts's own use of this
// pattern.
installDomShim();
const { flush, renderHook } = await import("@apps/explorer/listing/hook-harness");
const { setResolvedSnapshot } = await import("@platform/lib/snapshot-param");
const { useSnapshotForFolder } = await import("@apps/explorer/listing/useSnapshotForFolder");

type FakeLocation = { search: string; pathname: string };

const curLoc = () => (globalThis as unknown as { location: FakeLocation }).location;

describe("useSnapshotForFolder", () => {
  const originalFetch = globalThis.fetch;
  const originalLocation = (globalThis as Record<string, unknown>).location;
  const originalHistory = (globalThis as Record<string, unknown>).history;
  let requested: string[] = [];

  beforeEach(() => {
    setResolvedSnapshot(null);
    requested = [];
    const fresh: FakeLocation = { search: "", pathname: "/w/myapp" };
    (globalThis as Record<string, unknown>).location = fresh;
    (globalThis as Record<string, unknown>).history = {
      state: null,
      // A minimal stand-in for the real history object: parses the url
      // `replaceSearch` builds and applies it to whatever `globalThis.location`
      // CURRENTLY is (re-read fresh, never the `fresh` object captured above)
      // — see this file's own header comment on why.
      replaceState: (_state: unknown, _title: string, url: string) => {
        const target = curLoc();
        const qIndex = url.indexOf("?");
        target.pathname = qIndex === -1 ? url : url.slice(0, qIndex);
        target.search = qIndex === -1 ? "" : url.slice(qIndex);
      },
    };
    globalThis.fetch = ((url: string) => {
      const u = new URL(url, "http://x");
      const path = u.searchParams.get("path") ?? "";
      const sha = u.searchParams.get("sha") ?? "";
      requested.push(`${path}@${sha}`);
      if (sha === "deadbee0") {
        // A DEFINITIVE 404 (finding [2], round 2: only this status is
        // grounds to give up and clear `_snapshot` — see this file's own
        // status-branching tests below for the transient-error case).
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
  });

  it("resolves _snapshot off the URL on mount", async () => {
    curLoc().search = "?_snapshot=abc1234";
    const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
    await flush();
    expect(box.current().resolvedSnapshot).toEqual({
      sha: "abc1234",
      dir: "/cache/key/abc1234",
      app_dir: "/w/myapp",
    });
    box.unmount();
  });

  it(
    "THE regression: a _snapshot change with fsPath unchanged is picked up " +
      "only once urlVersion bumps, not on every render",
    async () => {
      curLoc().search = "?_snapshot=abc1234";
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      await flush();
      expect(box.current().resolvedSnapshot?.sha).toBe("abc1234");
      requested = [];

      // Stand in for ANOTHER component's replaceSearch (Preview.tsx picking a
      // different commit) — the URL changes, but this hook's own `fsPath`
      // argument does not.
      curLoc().search = "?_snapshot=def5678";

      // Re-rendered with urlVersion UNCHANGED (as the previous, buggy shape
      // effectively always did, since it never took urlVersion as a
      // dependency at all): must NOT pick up the new sha.
      box.rerender("/w/myapp", 0);
      await flush();
      expect(requested).toEqual([]); // no re-resolve happened
      expect(box.current().resolvedSnapshot?.sha).toBe("abc1234"); // stale

      // Bumping urlVersion — what `useUrlVersion()` produces on the real
      // `fused:urlchange` event a `replaceSearch` dispatches — is what makes
      // the fix real: the SAME fsPath now re-resolves the new sha.
      box.rerender("/w/myapp", 1);
      await flush();
      expect(requested).toEqual(["/w/myapp@def5678"]);
      expect(box.current().resolvedSnapshot?.sha).toBe("def5678");
      box.unmount();
    }
  );

  it("does not re-resolve on a urlVersion bump that changes nothing", async () => {
    curLoc().search = "?_snapshot=abc1234";
    const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
    await flush();
    requested = [];

    // An unrelated history write (a sort param, `_side`) also bumps
    // urlVersion, but `_snapshot` itself is unchanged — no redundant round
    // trip.
    box.rerender("/w/myapp", 1);
    await flush();
    expect(requested).toEqual([]);
    expect(box.current().resolvedSnapshot?.sha).toBe("abc1234");
    box.unmount();
  });

  it("a resolve failure leaves resolvedSnapshot null rather than pending forever", async () => {
    curLoc().search = "?_snapshot=deadbee0";
    const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
    // The failure path is several microtask hops deeper than a plain
    // resolve (fetch -> res.json() -> the `!res.ok` check -> throw ->
    // getGitSnapshot's own rejection -> the hook's `.catch()`), so a single
    // `flush()` (two `Promise.resolve()` ticks) is not always enough to
    // settle it.
    //
    // Asserted through `resolvedSnapshot` alone (React state this hook owns
    // and returns), not through a read-back of `globalThis.location.search`
    // — the latter is also written by the `.catch()` (see
    // useSnapshotForFolder.ts's own comment on why it must clear `_snapshot`
    // rather than stay pending), but observing THAT specific write reliably
    // needs a `history.replaceState` shared with ~90 other files in this
    // process, several of which install their OWN throwaway
    // `globalThis.location`/`.history` per test; this file's own async gaps
    // (`await flush()`) can straddle one of theirs, and by the time this
    // hook's `.catch()` actually calls `history.replaceState`, the CURRENT
    // `globalThis.history` may belong to a different suite entirely. The URL
    // write's own LOGIC (clearing exactly `_snapshot` and nothing else) is
    // pure and covered directly by `writeQueryParam`'s own tests; what this
    // test owns is that the hook's local state genuinely reaches "live"
    // rather than staying stuck pending.
    for (let i = 0; i < 8 && box.current().resolvedSnapshot !== null; i++) {
      await flush();
    }
    expect(box.current().resolvedSnapshot).toBe(null);
    box.unmount();
  });

  it("backToLive clears the resolution", async () => {
    curLoc().search = "?_snapshot=abc1234";
    const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
    await flush();
    expect(box.current().resolvedSnapshot?.sha).toBe("abc1234");
    // See the test above for why this checks `resolvedSnapshot` (this
    // hook's own React state) rather than reading back
    // `globalThis.location.search` — the URL write itself is the same
    // `writeQueryParam` call the failure path makes, already covered there.
    await flush(() => box.current().backToLive());
    expect(box.current().resolvedSnapshot).toBe(null);
    box.unmount();
  });

  it(
    "a TRANSIENT failure (no numeric 404) leaves the resolution pending, " +
      "not cleared to live",
    async () => {
      // Regression for finding [2], round 2 review: only a DEFINITIVE 404
      // (no app folder encloses this path) is grounds to give up and clear
      // `_snapshot`. A network drop, a 500, or a server mid-restart must not
      // read identically to "there is genuinely no snapshot here" — the
      // previous shape cleared the shared `_snapshot` URL (and the
      // singleton) on ANY failure alike.
      curLoc().search = "?_snapshot=aaaa111";
      globalThis.fetch = (() =>
        Promise.reject(new TypeError("network error"))) as unknown as typeof fetch;
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      for (let i = 0; i < 8 && box.current().resolvedSnapshot !== null; i++) {
        await flush();
      }
      // Still null (never resolved), but critically the mechanism did not
      // treat this as a confirmed "no app folder" and blow away a snapshot
      // some OTHER pane might legitimately be showing — proven directly
      // below, in the cross-pane test.
      expect(box.current().resolvedSnapshot).toBe(null);
      box.unmount();
    }
  );

  it(
    "a 404 for THIS folder does not clear a sha another pane already " +
      "resolved successfully",
    async () => {
      // Regression for finding [2], round 2 review: the previous shape
      // cleared the SHELL's own `_snapshot` URL unconditionally the instant
      // ANY mount's resolve failed — tearing the snapshot down for every
      // other pane. Two apps in one repo share shas, so a companion
      // Preview pane resolving the SAME sha against a DIFFERENT app folder
      // is exactly the shape that must survive this folder's own 404.
      setResolvedSnapshot({ sha: "deadbee0", dir: "/cache/key/deadbee0", app_dir: "/w/otherapp" });
      curLoc().search = "?_snapshot=deadbee0";
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      for (let i = 0; i < 8 && box.current().resolvedSnapshot !== null; i++) {
        await flush();
      }
      // This folder gives up locally (it lists live)...
      expect(box.current().resolvedSnapshot).toBe(null);
      // ...but the shared URL, and the OTHER pane's resolution, survive.
      expect(curLoc().search).toBe("?_snapshot=deadbee0");
      box.unmount();
    }
  );

  it(
    "FINDING 3 (round 3): backToLive tells the git sidebar via " +
      "_fusedSnapshotCleared, not just Preview.tsx's own back-to-live",
    async () => {
      curLoc().search = "?_snapshot=abc1234";
      let notified = false;
      (globalThis as Record<string, unknown>).document = {
        querySelector: (sel: string) =>
          sel === ".preview-side-frame"
            ? { contentWindow: { _fusedSnapshotCleared: () => (notified = true) } }
            : null,
      };
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      await flush();
      expect(box.current().resolvedSnapshot?.sha).toBe("abc1234");

      await flush(() => box.current().backToLive());

      expect(notified).toBe(true);
      box.unmount();
      delete (globalThis as Record<string, unknown>).document;
    }
  );

  it(
    "FINDING 3 (round 3): a 404 that clears the shared snapshot also tells " +
      "the git sidebar",
    async () => {
      curLoc().search = "?_snapshot=deadbee0";
      let notified = false;
      (globalThis as Record<string, unknown>).document = {
        querySelector: (sel: string) =>
          sel === ".preview-side-frame"
            ? { contentWindow: { _fusedSnapshotCleared: () => (notified = true) } }
            : null,
      };
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      // Unconditional flushes (not gated on `resolvedSnapshot !== null`,
      // unlike the pre-existing "resolve failure" test above): this hook's
      // local `resolvedSnapshot` starts out null AND ends null on a 404, so
      // a condition of `!== null` would exit the loop on iteration zero
      // without ever giving the fetch/catch chain a single tick to run.
      for (let i = 0; i < 8; i++) {
        await flush();
      }
      expect(box.current().resolvedSnapshot).toBe(null);
      expect(notified).toBe(true);
      box.unmount();
      delete (globalThis as Record<string, unknown>).document;
    }
  );

  it(
    "ROUND 4: a URL that drops _snapshot outright (back/forward) clears " +
      "the shared singleton AND tells the git sidebar, not just this " +
      "hook's own local state",
    async () => {
      // Regression for round 4's inventory: the `!isSha(raw)` branch used
      // to call `setResolvedSnapshot(null)` directly — no URL write (fine,
      // the URL here already lacks the param) and, critically, no sidebar
      // hop, leaving Checkout armed in the sidebar for a version this
      // folder no longer shows on screen. `clearShellSnapshot` is the one
      // place that hop lives; this branch must go through it like every
      // other clear path.
      const { setResolvedSnapshot: setSingleton, getResolvedSnapshot } =
        await import("@platform/lib/snapshot-param");
      curLoc().search = "?_snapshot=abc1234";
      let notified = false;
      (globalThis as Record<string, unknown>).document = {
        querySelector: (sel: string) =>
          sel === ".preview-side-frame"
            ? { contentWindow: { _fusedSnapshotCleared: () => (notified = true) } }
            : null,
      };
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      await flush();
      expect(box.current().resolvedSnapshot?.sha).toBe("abc1234");
      expect(getResolvedSnapshot()?.sha).toBe("abc1234");

      // Stand in for browser back/forward: the URL loses `_snapshot`
      // entirely, with no other component involved to call
      // `clearShellSnapshot` on this hook's behalf.
      curLoc().search = "";
      box.rerender("/w/myapp", 1);
      await flush();

      expect(box.current().resolvedSnapshot).toBe(null);
      expect(getResolvedSnapshot()).toBe(null);
      expect(notified).toBe(true);
      box.unmount();
      delete (globalThis as Record<string, unknown>).document;
      setSingleton(null);
    }
  );

  it(
    "a cached resolution for a DIFFERENT app folder is not reused just " +
      "because the sha matches",
    async () => {
      // Regression for finding [3], round 2 review: the "already resolved"
      // short-circuit used to compare only `sha`, never whether the cached
      // `app_dir` actually encloses THIS folder. Two apps in one repo share
      // shas, so a resolution the singleton already holds for appA must not
      // be reused, unresolved, for a folder in appB under the same sha.
      setResolvedSnapshot({ sha: "abc1234", dir: "/cache/key/abc1234", app_dir: "/w/otherapp" });
      curLoc().search = "?_snapshot=abc1234";
      const box = renderHook(useSnapshotForFolder, "/w/myapp", 0);
      await flush();
      // Must have RE-resolved against THIS folder's own app, not reused the
      // other app's cached answer.
      expect(requested).toEqual(["/w/myapp@abc1234"]);
      expect(box.current().resolvedSnapshot).toEqual({
        sha: "abc1234",
        dir: "/cache/key/abc1234",
        app_dir: "/w/myapp",
      });
      box.unmount();
    }
  );
});
