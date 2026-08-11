import { describe, expect, it } from "bun:test";
import { reposMessage, reposView, type ReposView } from "@apps/explorer/lib/repos";
import type { GitRepos } from "@platform/lib/api";

const msg = (v: ReposView) => reposMessage(v);
const res = (over: Partial<GitRepos> = {}): GitRepos => ({
  indexed: false,
  scanning: false,
  repos: [],
  ...over,
});

describe("reposView", () => {
  it("is loading until a response arrives, which is not the same as empty", () => {
    expect(reposView(null, false, null)).toEqual({ kind: "loading" });
    expect(msg(reposView(null, false, null))).toMatch(/Looking for repos/);
  });

  it("reports a failed request rather than an empty machine", () => {
    expect(reposView(null, true, null)).toEqual({ kind: "failed" });
    // even with a response in hand, a later failure is not "no repos"
    expect(reposView(res({ indexed: true }), true, null).kind).toBe("failed");
  });

  it("an indexed answer is ready, empty list included", () => {
    const v = reposView(res({ indexed: true }), false, null);
    expect(v).toEqual({ kind: "ready", repos: [] });
    expect(msg(v)).toMatch(/No git repositories/);
  });

  it("keeps serving a real answer while a RESCAN runs", () => {
    // A rescan over a usable index keeps serving the last completed generation
    // (index-store.md §4), so scanning must not downgrade an answer to "wait".
    const v = reposView(res({ indexed: true, repos: [{ path: "/a" }] }), false, true);
    expect(v).toEqual({ kind: "ready", repos: [{ path: "/a" }] });
  });

  it("the live poll outranks the response's older scanning flag", () => {
    // response says idle, poll says a scan just started
    expect(reposView(res(), false, true).kind).toBe("building");
    // response says scanning, poll says it has stopped — see the four endings below
    expect(reposView(res({ scanning: true }), false, false).kind).toBe("unavailable");
    // ...and with nothing polled yet, the response's own flag is all there is
    expect(reposView(res({ scanning: true }), false, null).kind).toBe("building");
  });
});

// The four ways a scan can end, from the tab's point of view. Only the first
// updates the manifest, which is why "a scan completed" was the wrong refetch
// trigger and the pair (scanning, last_completed_at) is the right one — see
// FilesHome. Here the contract is narrower and total: given a fresh response and
// the live flag, the state is never "building" once nothing is scanning.
describe("the four scan endings", () => {
  it("COMPLETED: the index can answer, so the list shows", () => {
    const v = reposView(res({ indexed: true, repos: [{ path: "/r" }] }), false, false);
    expect(v).toEqual({ kind: "ready", repos: [{ path: "/r" }] });
  });

  it("CANCELLED: no index and nothing running — actionable, not 'building'", () => {
    // The regression this replaced: scanning went true -> false with the manifest
    // untouched, so the old clamp held "Still building…" forever.
    const v = reposView(res({ indexed: false, scanning: false }), false, false);
    expect(v.kind).toBe("unavailable");
    expect(msg(v)).toMatch(/Preferences → Indexing/);
    expect(msg(v)).not.toMatch(/Still building/);
  });

  it("FAILED: indistinguishable from cancelled here, and should be", () => {
    // A failed run also stops without a manifest. The tab has no better advice
    // than "rebuild it", so both endings share one honest message.
    const v = reposView(res({ indexed: false, scanning: false }), false, false);
    expect(v.kind).toBe("unavailable");
  });

  it("NEVER STARTED: same state, reached without any scan at all", () => {
    const v = reposView(res(), false, null);
    expect(v.kind).toBe("unavailable");
    expect(msg(v)).toMatch(/hasn't been built yet/);
  });

  it("IN FLIGHT: only this one says 'building'", () => {
    expect(msg(reposView(res({ scanning: true }), false, true))).toMatch(
      /Still building/,
    );
  });
});

describe("reposMessage", () => {
  it("gives every state its own copy, and no state falls through", () => {
    const kinds: ReposView[] = [
      { kind: "loading" },
      { kind: "failed" },
      { kind: "building" },
      { kind: "unavailable" },
      { kind: "ready", repos: [] },
    ];
    const seen = kinds.map(msg);
    expect(seen.every((m) => m.length > 0)).toBe(true);
    expect(new Set(seen).size).toBe(kinds.length);
  });
});
