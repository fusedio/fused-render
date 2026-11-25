// useAppPagePaneTab.ts — the app page's right-hand pane's Claude | Git strip:
// which tab is up and the borrowed `git` probe. AppPage.tsx itself has no
// render-test precedent (see AppPage.test.tsx's own header), so the piece of
// it with real behaviour to pin gets its own hook and its own test, driven
// through the REAL code path (useDirMode's real fetches, stubbed at the
// network boundary) rather than hand-assigned state.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement, type ReactElement } from "react";
import type { AppPagePaneTab } from "@shell/useAppPagePaneTab";

// `@apps/explorer/lib/dir-mode` pulls in `@platform/lib/api`, whose module
// init (through `@platform/lib/router`'s legacy-URL rewrite) reads `location`
// at import time — the same minimal stand-in appdoctor-lib.test.ts installs
// for the same reason, put back once the import resolves so a leaked global
// does not reach another file in this same `bun test` process.
// The shared DOM stub, installed and LEFT STANDING (see testDomShim.ts).
// This file used to stash the `location`/`history` descriptors, assign its
// own, and put the originals back — deleting them when there were none. Every
// suite runs in ONE bun process, so that delete pulled `location` out from
// under whichever file ran next and had already installed the shim: 160
// `ReferenceError: location is not defined` on CI, none locally, purely by
// file order (2026-09-24).
installDomShim();
const { useAppPagePaneTab } = await import("@shell/useAppPagePaneTab");
const { setSideTab, getSideTab } = await import("@apps/explorer/lib/side-tab-store");

// ---- a tiny local hook harness, mirroring AppPage.test.tsx's own -----------

function renderHook(dir: string, enabled = true): {
  current: () => AppPagePaneTab;
  setEnabled: (on: boolean) => void;
  unmount: () => void;
} {
  let latest: AppPagePaneTab;
  let renderer: ReactTestRenderer;
  function Probe(props: { dir: string; enabled: boolean }): ReactElement | null {
    latest = useAppPagePaneTab(props.dir, props.enabled);
    return null;
  }
  act(() => {
    renderer = create(createElement(Probe, { dir, enabled }));
  });
  return {
    current: () => latest,
    setEnabled: (on) => {
      act(() => {
        renderer.update(createElement(Probe, { dir, enabled: on }));
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

// The store is one module shared by every suite in this bun process: leave it
// as found (side-tab-store.test.ts expects it empty).
afterEach(() => setSideTab(null));

beforeEach(() => {
  setSideTab(null);
  calls = [];
  statPlan = {};
  failPlan = new Set();
  installFetch();
});

const GIT = { mode: "git", path: "/templates/git", icon: null };

// ------------------------------------------------------------- pane shut

test("makes no probe at all while the pane is shut", async () => {
  const dir = "/repo/app-shut";
  statPlan[dir] = { templates: [GIT] };
  const box = renderHook(dir, false);
  await flush();
  expect(box.current().tab).toBe("claude");
  expect(calls).toEqual([]);
  box.unmount();
});

// ------------------------------------------------------------- default tab

test("opens on Claude, and probes the FOLDER once for both tabs", async () => {
  const dir = "/repo/app-default";
  statPlan[dir] = { templates: [GIT] };
  const box = renderHook(dir);
  expect(box.current().tab).toBe("claude");
  expect(box.current().gitMode.pending).toBe(true);
  await flush();
  expect(calls).toEqual(["/api/fs/stat?path=" + encodeURIComponent(dir)]);
  expect(box.current().gitMode.pending).toBe(false);
  expect(box.current().tab).toBe("claude");
  box.unmount();
});

// ------------------------------------------------------------- switching

test("showTab puts Git up and frames the git template's src against the folder", async () => {
  const dir = "/repo/app-opens";
  statPlan[dir] = { templates: [GIT] };
  const box = renderHook(dir);
  act(() => box.current().showTab("git"));
  expect(box.current().tab).toBe("git");
  // A door, not the reader's pick: the explorer's remembered tab is untouched.
  expect(getSideTab()).toBeNull();
  await flush();
  expect(box.current().gitSrc).toBe(
    "/render?path=" + encodeURIComponent("/templates/git") + "&_file=" + encodeURIComponent(dir),
  );
  expect(box.current().tab).toBe("git");
  box.unmount();
});

test("a strip press switches back to Claude and is remembered with the explorer's tab", async () => {
  const dir = "/repo/app-switch";
  statPlan[dir] = { templates: [GIT] };
  const box = renderHook(dir);
  act(() => box.current().selectTab("git"));
  expect(getSideTab()).toBe("git");
  act(() => box.current().selectTab("claude"));
  expect(box.current().tab).toBe("claude");
  expect(getSideTab()).toBe("claude");
  box.unmount();
});

test("starts on Git when Git was the tab last picked (here or in the explorer)", async () => {
  const dir = "/repo/app-remembered";
  statPlan[dir] = { templates: [GIT] };
  setSideTab("git");
  const box = renderHook(dir);
  expect(box.current().tab).toBe("git");
  await flush();
  expect(box.current().tab).toBe("git");
  box.unmount();
});

// ------------------------------------------------------------- fall back

test("falls back to Claude once the folder settles as not offering git", async () => {
  const dir = "/repo/app-no-git";
  statPlan[dir] = { templates: [] };
  const box = renderHook(dir);
  act(() => box.current().showTab("git"));
  expect(box.current().tab).toBe("git");
  await flush();
  expect(box.current().gitMode.entry).toBeNull();
  expect(box.current().tab).toBe("claude");
  box.unmount();
});

test("does not fall back during the commit the pane opens on (ABSENT before the probe starts)", async () => {
  const dir = "/repo/app-reopen";
  statPlan[dir] = { templates: [GIT] };
  setSideTab("git");
  const box = renderHook(dir, false);
  expect(box.current().tab).toBe("git");
  box.setEnabled(true);
  expect(box.current().tab).toBe("git");
  await flush();
  expect(box.current().tab).toBe("git");
  expect(box.current().gitSrc).not.toBeNull();
  box.unmount();
});

test("does not fall back on a probe that failed — a rejected fetch is not a settled 'no git'", async () => {
  const dir = "/repo/app-probe-fails";
  failPlan.add(dir);
  const box = renderHook(dir);
  act(() => box.current().showTab("git"));
  await flush();
  expect(box.current().gitMode.entry).toBeNull();
  expect(box.current().gitMode.failed).toBe(true);
  expect(box.current().tab).toBe("git");
  box.unmount();
});
