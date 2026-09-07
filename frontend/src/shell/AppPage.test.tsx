// AppPage's own `_snapshot` resolve/gate behavior (useAppPageSnapshot.ts).
//
// AppPage.tsx ITSELF has no render-test precedent in this codebase and is not
// attempted here: mounting it pulls in base-ui's Tabs, a document-dependent
// keyboard-nav effect, useFavicon, and the Tasks page's whole subtree — the
// same "no render-test precedent, extensive unrelated mocking" reasoning the
// explorer's own Listing.tsx (2100 lines) and Preview.tsx (2570 lines) were
// given for the identical mechanism on this branch (see
// DECISIONS-app-snapshot-preview.md, Task 5 and the Review pass). What DOES
// get a genuine test, through the REAL code path rather than hand-assigned
// state (per this branch's own review history — two rounds each caught a
// test that assigned "resolved" state directly instead of driving the actual
// resolve): `useAppPageSnapshot`, extracted out of AppPage.tsx's own body for
// exactly this reason, driven through a small local hook harness
// (react-test-renderer, no DOM — mirrors the explorer's own
// listing/hook-harness.ts, inlined here rather than imported across the
// shell/apps boundary for one small helper).
//
// `window`/`location`/`history` are the minimal globals `replaceSearch`
// (`history.replaceState`) touches — installed once at file load, mirroring
// RepoUpdatesDock.test.tsx's own router.ts precedent.
import { beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement, type ReactElement } from "react";

let currentUrl = { pathname: "/apps/repo/myapp", search: "" };
let replaced: string[] = [];

(globalThis as Record<string, unknown>).location = currentUrl;
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: (_state: unknown, _title: string, url: string) => {
    replaced.push(url);
    const [pathname, search] = url.split("?");
    currentUrl = { pathname, search: search ? "?" + search : "" };
    (globalThis as Record<string, unknown>).location = currentUrl;
  },
};

const { useAppPageSnapshot } = await import("@shell/useAppPageSnapshot");
import type { AppPageSnapshotState } from "@shell/useAppPageSnapshot";

// ---- a tiny local hook harness (react-test-renderer, no DOM) ----------------
// Mirrors listing/hook-harness.ts's own `renderHook`/`flush`, inlined rather
// than imported across the shell/apps boundary for this one small helper.

function renderHook(dir: string, urlVersion: number): {
  current: () => AppPageSnapshotState;
  rerender: (dir: string, urlVersion: number) => void;
  unmount: () => void;
} {
  let latest: AppPageSnapshotState = { sha: null, snap: null, pending: false };
  let renderer: ReactTestRenderer;
  function Probe(props: { dir: string; urlVersion: number }): ReactElement | null {
    latest = useAppPageSnapshot(props.dir, props.urlVersion);
    return null;
  }
  act(() => {
    renderer = create(createElement(Probe, { dir, urlVersion }));
  });
  return {
    current: () => latest,
    rerender: (nextDir: string, nextVersion: number) => {
      act(() => {
        renderer.update(createElement(Probe, { dir: nextDir, urlVersion: nextVersion }));
      });
    },
    unmount: () => {
      act(() => {
        renderer.unmount();
      });
    },
  };
}

async function flush(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

// ---- fixtures ----------------------------------------------------------------

const APP = "/repo/myapp";
const OTHER_APP = "/repo/otherapp";
const SHA = "a".repeat(40);
const SHA2 = "b".repeat(40);

type SnapshotReply =
  | { kind: "ok"; dir: string; app_dir: string; entry?: string | null }
  | { kind: "404" }
  | { kind: "error" };

let plan: Record<string, SnapshotReply> = {};
let calls: string[] = [];

function installFetch() {
  (globalThis as Record<string, unknown>).fetch = (async (url: string) => {
    const u = new URL(url, "http://x");
    const path = u.searchParams.get("path")!;
    const sha = u.searchParams.get("sha")!;
    calls.push(path + "@" + sha);
    const reply = plan[path + "@" + sha] ?? { kind: "404" as const };
    if (reply.kind === "error") throw new Error("network down");
    if (reply.kind === "404") {
      return { ok: false, status: 404, json: async () => ({ error: "no app folder" }) };
    }
    return {
      ok: true,
      json: async () => ({
        ok: true,
        dir: reply.dir,
        entry: reply.entry ?? null,
        app_dir: reply.app_dir,
      }),
    };
  }) as typeof fetch;
}

beforeEach(() => {
  currentUrl = { pathname: "/apps/repo/myapp", search: "" };
  (globalThis as Record<string, unknown>).location = currentUrl;
  replaced = [];
  plan = {};
  calls = [];
  installFetch();
});

function setSearch(search: string) {
  currentUrl = { pathname: currentUrl.pathname, search };
  (globalThis as Record<string, unknown>).location = currentUrl;
}

// -------------------------------------------------------------------- "Live"

test("no _snapshot on the URL resolves to live (sha/snap null, not pending), no fetch at all", async () => {
  const box = renderHook(APP, 0);
  await flush();
  expect(box.current()).toEqual({ sha: null, snap: null, pending: false });
  expect(calls).toEqual([]);
  box.unmount();
});

// ---------------------------------------------------------------- resolving

test("finding 4: a sha on the URL reads as PENDING, not live, before the resolve lands", async () => {
  setSearch("?_snapshot=" + SHA);
  plan[APP + "@" + SHA] = { kind: "ok", dir: "/cache/key/" + SHA, app_dir: APP };
  // Deliberately do not await `flush()` yet — the fetch stub above is async,
  // so the microtask it returns has not settled. A caller reading `snap` as
  // "live" here (the old contract: null meant live OR pending, indistinguishably)
  // would mount a frame against the live app before the resolve swaps it out.
  const box = renderHook(APP, 0);
  expect(box.current().snap).toBeNull();
  expect(box.current().pending).toBe(true);
  expect(box.current().sha).toBe(SHA);
  await flush();
  box.unmount();
});

test("a valid sha resolves via GET /api/git/snapshot for THIS page's own dir, carrying `entry` (finding 3)", async () => {
  setSearch("?_snapshot=" + SHA);
  plan[APP + "@" + SHA] = {
    kind: "ok",
    dir: "/cache/key/" + SHA,
    app_dir: APP,
    entry: "/cache/key/" + SHA + "/main.html",
  };
  const box = renderHook(APP, 0);
  await flush();
  expect(box.current()).toEqual({
    sha: SHA,
    snap: {
      sha: SHA,
      dir: "/cache/key/" + SHA,
      app_dir: APP,
      entry: "/cache/key/" + SHA + "/main.html",
    },
    pending: false,
  });
  expect(calls).toEqual([APP + "@" + SHA]);
  box.unmount();
});

test("an already-resolved sha for an app folder that still encloses dir is not re-fetched", async () => {
  setSearch("?_snapshot=" + SHA);
  plan[APP + "@" + SHA] = { kind: "ok", dir: "/cache/key/" + SHA, app_dir: APP };
  const box = renderHook(APP, 0);
  await flush();
  expect(calls.length).toBe(1);
  // A re-render with the SAME dir/urlVersion (e.g. a parent re-render for an
  // unrelated reason) must not re-issue the request.
  box.rerender(APP, 0);
  await flush();
  expect(calls.length).toBe(1);
  box.unmount();
});

test("finding 3 (round 2): a resolution for the right sha but a DIFFERENT app folder is treated as unresolved", async () => {
  // Resolve SHA against APP first.
  setSearch("?_snapshot=" + SHA);
  plan[APP + "@" + SHA] = { kind: "ok", dir: "/cache/key1/" + SHA, app_dir: APP };
  plan[OTHER_APP + "@" + SHA] = { kind: "ok", dir: "/cache/key2/" + SHA, app_dir: OTHER_APP };
  const box = renderHook(APP, 0);
  await flush();
  expect(box.current().snap?.app_dir).toBe(APP);
  expect(calls).toEqual([APP + "@" + SHA]);

  // The SAME sha is still on the URL, but `dir` switched to a DIFFERENT app
  // folder the stale resolution's app_dir does not enclose — must re-fetch
  // for the new dir rather than silently reusing APP's resolution, and reads
  // as pending in between rather than as APP's now-stale snap.
  box.rerender(OTHER_APP, 1);
  await flush();
  expect(calls).toEqual([APP + "@" + SHA, OTHER_APP + "@" + SHA]);
  expect(box.current().snap?.app_dir).toBe(OTHER_APP);
  expect(box.current().pending).toBe(false);
  box.unmount();
});

// ------------------------------------------------------------------- failure

test("a confirmed 404 falls back to Live and clears _snapshot from the URL", async () => {
  setSearch("?_snapshot=" + SHA);
  // No plan entry -> the fetch stub's default is a 404.
  const box = renderHook(APP, 0);
  await flush();
  expect(replaced.length).toBe(1);
  expect(replaced[0]).not.toContain("_snapshot");
  // In the real app, `replaceSearch` dispatches `fused:urlchange`, which
  // AppPage's own `useUrlVersion()` turns into a fresh `urlVersion` prop —
  // this hook's `sha` is read fresh from `location.search` on every render,
  // so that prop bump is what makes it see the now-cleared URL. This bare
  // hook harness has no such listener wired up, so the rerender is done by
  // hand here to stand in for it (React also bails out of the no-op
  // `setResolvedSnapshot(null)` above, since `resolvedSnapshot` state was
  // already null — the URL write is the only thing that actually changed).
  box.rerender(APP, 1);
  await flush();
  expect(box.current()).toEqual({ sha: null, snap: null, pending: false });
  box.unmount();
});

test("a transient failure (not a confirmed 404) leaves the prior resolution's cache entry as it was; the URL's NEW sha reads as pending, never as the stale snap", async () => {
  // First resolve successfully...
  setSearch("?_snapshot=" + SHA);
  plan[APP + "@" + SHA] = { kind: "ok", dir: "/cache/key/" + SHA, app_dir: APP };
  const box = renderHook(APP, 0);
  await flush();
  expect(box.current().snap?.sha).toBe(SHA);

  // ...then a re-render for an unrelated reason re-runs against a NEW sha
  // whose fetch fails transiently (not 404) — the URL must be left alone,
  // and the returned state must not claim the OLD sha's snap is still the
  // answer for a URL that now names SHA2: that would render a THIRD era
  // (neither SHA2, which is what the URL claims, nor honestly live).
  setSearch("?_snapshot=" + SHA2);
  plan[APP + "@" + SHA2] = { kind: "error" };
  box.rerender(APP, 1);
  await flush();
  expect(box.current().sha).toBe(SHA2);
  expect(box.current().snap).toBeNull();
  expect(box.current().pending).toBe(true);
  expect(replaced.length).toBe(0);
  box.unmount();
});
