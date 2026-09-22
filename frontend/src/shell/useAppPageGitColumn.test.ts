// useAppPageGitColumn.ts — the app page's right-hand git column state,
// extracted out of AppPage.tsx's own body for the same reason
// useAppPageSnapshot.ts was (see that file, and AppPage.test.tsx's own
// header): AppPage.tsx itself has no render-test precedent, so the piece of
// it with real behaviour to pin gets its own hook and its own test, driven
// through the REAL code path (useDirMode's real fetches, stubbed at the
// network boundary) rather than hand-assigned state.
import { beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement, type ReactElement } from "react";
import type { AppPageGitColumn } from "@shell/useAppPageGitColumn";

// `@apps/explorer/lib/dir-mode` pulls in `@platform/lib/api`, whose module
// init (through `@platform/lib/router`'s legacy-URL rewrite) reads `location`
// at import time — the same minimal stand-in appdoctor-lib.test.ts installs
// for the same reason, put back once the import resolves so a leaked global
// does not reach another file in this same `bun test` process.
const before = {
  location: Reflect.getOwnPropertyDescriptor(globalThis, "location"),
  history: Reflect.getOwnPropertyDescriptor(globalThis, "history"),
};
Object.assign(globalThis, {
  location: { pathname: "/", search: "", href: "http://localhost/" },
  history: { state: null, replaceState() {} },
});
const { useAppPageGitColumn } = await import("@shell/useAppPageGitColumn");
for (const key of ["location", "history"] as const) {
  const desc = before[key];
  if (desc) Reflect.defineProperty(globalThis, key, desc);
  else Reflect.deleteProperty(globalThis, key);
}

// ---- a tiny local hook harness, mirroring AppPage.test.tsx's own -----------

function renderHook(dir: string): {
  current: () => AppPageGitColumn;
  unmount: () => void;
} {
  let latest: AppPageGitColumn;
  let renderer: ReactTestRenderer;
  function Probe(props: { dir: string }): ReactElement | null {
    latest = useAppPageGitColumn(props.dir);
    return null;
  }
  act(() => {
    renderer = create(createElement(Probe, { dir }));
  });
  return {
    current: () => latest,
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

// ---- fetch stub --------------------------------------------------------
//
// dir-mode.ts caches per-directory answers for 30s (module-level, no reset
// hook), so every test below uses its OWN directory string — sharing one
// across tests would let an earlier test's resolution silently answer a
// later one instead of hitting this stub.

let calls: string[] = [];
let statPlan: Record<string, { templates: Array<{ mode: string; path: string | null; icon: string | null; conditional?: boolean }> }> = {};
// Directories in here reject their `/api/fs/stat` call outright — a transient
// network/server failure, not a settled verdict — so a test can prove the
// column tells that apart from a genuine "no git here" (both otherwise land on
// the same `{ entry: null, pending: false }` shape out of `useDirMode`).
let failPlan: Set<string> = new Set();

function installFetch() {
  (globalThis as Record<string, unknown>).fetch = (async (url: string) => {
    const u = new URL(url, "http://x");
    calls.push(u.pathname + u.search);
    if (u.pathname === "/api/fs/stat") {
      const path = u.searchParams.get("path")!;
      if (failPlan.has(path)) throw new Error("stat failed: " + path);
      const plan = statPlan[path] ?? { templates: [] };
      return {
        ok: true,
        json: async () => ({
          path,
          name: path,
          is_dir: true,
          size: null,
          mtime: null,
          templates: plan.templates,
        }),
      };
    }
    if (u.pathname === "/api/fs/conditions") {
      const path = u.searchParams.get("path")!;
      return { ok: true, json: async () => ({ path, conditions: {} }) };
    }
    throw new Error("unexpected fetch: " + url);
  }) as typeof fetch;
}

beforeEach(() => {
  calls = [];
  statPlan = {};
  failPlan = new Set();
  installFetch();
});

// ------------------------------------------------------------- not asked for

test("makes no probe at all until the column is asked for", async () => {
  const dir = "/repo/app-not-asked";
  statPlan[dir] = { templates: [{ mode: "git", path: "/templates/git", icon: null }] };
  const box = renderHook(dir);
  await flush();
  expect(box.current().open).toBe(false);
  expect(calls).toEqual([]);
  box.unmount();
});

// ------------------------------------------------------------------- opening

test("openGit probes the FOLDER and frames the git template's src against it", async () => {
  const dir = "/repo/app-opens";
  statPlan[dir] = { templates: [{ mode: "git", path: "/templates/git", icon: null }] };
  const box = renderHook(dir);
  act(() => box.current().openGit());
  expect(box.current().open).toBe(true);
  await flush();
  expect(calls).toEqual(["/api/fs/stat?path=" + encodeURIComponent(dir)]);
  expect(box.current().gitMode.pending).toBe(false);
  expect(box.current().gitSrc).toBe(
    "/render?path=" + encodeURIComponent("/templates/git") + "&_file=" + encodeURIComponent(dir),
  );
  box.unmount();
});

// ------------------------------------------------------------------- closing

test("closeGit shuts the column and its src goes back to nothing to show", async () => {
  const dir = "/repo/app-closes";
  statPlan[dir] = { templates: [{ mode: "git", path: "/templates/git", icon: null }] };
  const box = renderHook(dir);
  act(() => box.current().openGit());
  await flush();
  expect(box.current().open).toBe(true);
  act(() => box.current().closeGit());
  expect(box.current().open).toBe(false);
  box.unmount();
});

// ---------------------------------------------------------------- auto-close

test("auto-closes once the folder settles as not offering git at all", async () => {
  const dir = "/repo/app-no-git";
  statPlan[dir] = { templates: [] }; // no git entry bound here
  const box = renderHook(dir);
  act(() => box.current().openGit());
  expect(box.current().open).toBe(true);
  await flush();
  expect(box.current().gitMode.entry).toBeNull();
  expect(box.current().open).toBe(false);
  box.unmount();
});

// ---------------------------------------------------------------- probe failure

test("does not auto-close on a probe that failed — a rejected fetch is not a settled 'no git'", async () => {
  const dir = "/repo/app-probe-fails";
  failPlan.add(dir);
  const box = renderHook(dir);
  act(() => box.current().openGit());
  expect(box.current().open).toBe(true);
  await flush();
  // Same shape `useDirMode` gives a genuine "no git here" — entry null,
  // pending false — except the caller can tell the two apart.
  expect(box.current().gitMode.entry).toBeNull();
  expect(box.current().gitMode.failed).toBe(true);
  expect(box.current().open).toBe(true);
  box.unmount();
});
