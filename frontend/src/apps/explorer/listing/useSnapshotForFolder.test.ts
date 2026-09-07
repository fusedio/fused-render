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
        return Promise.resolve({
          ok: false,
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
});
