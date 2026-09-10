// `usePaneState` ACROSS A TARGET CHANGE — the one thing the promise in `ready`
// does not cover.
//
// `ready` was always per-target, so every caller that AWAITS it has always been
// correct. `status`, `decision` and `noPane` are read SYNCHRONOUSLY, and they
// used to be left on the PREVIOUS file for the length of the new file's stat:
// the pane went on framing the old document, and `has_pane` answered `true` off
// a stale `status: "ready"` — which walks straight past the `"resolving" → null`
// guard in ClaudeChat that exists to stop a session being spawned without
// `mcp__fused_approvals__app_state` (Bugbot, PR #1061). The guard only ever
// protected the FIRST target, because only that one's state STARTS OUT
// resolving.
//
// Deliberately NOT `mock.module("@platform/lib/api", …)` — see the header of
// apps/explorer/FilesHome.render.test.tsx for why that broke CI process-wide.
// `statPath` is a thin `getJson` wrapper, so this stubs `globalThis.fetch`, the
// plain unfrozen global, and drives the round trip by hand.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import type { StatResult } from "@platform/lib/api";

const { usePaneState } = await import("./AppPane");

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const statFor = (path: string): StatResult => ({
  path,
  name: path.slice(path.lastIndexOf("/") + 1),
  is_dir: false,
  size: 10,
  mtime: 1,
  // One offerable entry, so `decidePane` answers a real `src` and the status
  // lands on "ready" rather than "none".
  templates: [{ mode: "code", path: "/t/code/template.html", icon: "/t/code/icon.svg" }],
});

/** Every /api/fs/stat call, held open until the test releases it — the window
 *  this whole file is about is precisely "the new stat has not landed yet". */
function heldFetch() {
  const pending: Array<{ path: string; send: () => void }> = [];
  globalThis.fetch = ((input: RequestInfo | URL) => {
    const url = String(input);
    const path = decodeURIComponent(url.slice(url.indexOf("path=") + 5));
    return new Promise((resolve) => {
      pending.push({
        path,
        send: () =>
          resolve(
            new Response(JSON.stringify(statFor(path)), {
              status: 200,
              headers: { "Content-Type": "application/json" },
            }),
          ),
      });
    });
  }) as typeof fetch;
  return pending;
}

/** The project's TS lib target predates `Array.prototype.at`. */
const last = <T,>(a: T[]): T => a[a.length - 1];

interface Seen {
  status: string;
  src: string | null;
  noPane: boolean;
}

/** Renders the hook and records what a synchronous reader would have seen on
 *  every render — which is how `has_pane` and the pane's `src` read it. */
function mount(file: string | null) {
  const seen: Seen[] = [];
  function Probe({ file }: { file: string | null }) {
    const pane = usePaneState({ file, chatOnly: false });
    seen.push({ status: pane.status, src: pane.decision?.src ?? null, noPane: pane.noPane });
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe, { file }));
  });
  return { seen, rerender: (next: string | null) => act(() => r.update(createElement(Probe, { file: next }))) };
}

test("a file hop resets the state in the same render, so nothing reads the old target", async () => {
  const pending = heldFetch();
  const { seen, rerender } = mount("/w/one.html");

  expect(last(seen)).toEqual({ status: "resolving", src: null, noPane: false });

  // The first target settles.
  await act(async () => {
    pending.find((p) => p.path === "/w/one.html")!.send();
    await Promise.resolve();
  });
  expect(last(seen).status).toBe("ready");
  expect(last(seen).src).toContain("one.html");

  // THE HOP. The second stat is still out, and this is the render every
  // synchronous reader gets — it must already describe the NEW target and not
  // the old one. Before the fix this read `{status: "ready", src: …one.html…}`,
  // which is `has_pane: true` for a pane that is framing someone else's file.
  rerender("/w/two.html");
  expect(last(seen)).toEqual({ status: "resolving", src: null, noPane: false });

  // And no stale frame was ever painted in between: no render after the hop
  // carried the first target's src.
  const afterHop = seen.slice(seen.findIndex((s) => s.src === null && s.status === "resolving" && seen.indexOf(s) > 1));
  expect(afterHop.some((s) => s.src?.includes("one.html"))).toBe(false);

  await act(async () => {
    pending.find((p) => p.path === "/w/two.html")!.send();
    await Promise.resolve();
  });
  expect(last(seen).status).toBe("ready");
  expect(last(seen).src).toContain("two.html");
});

test("a hop to a null target reads as no-pane at once, not as the old file's pane", async () => {
  const pending = heldFetch();
  const { seen, rerender } = mount("/w/one.html");
  await act(async () => {
    pending.find((p) => p.path === "/w/one.html")!.send();
    await Promise.resolve();
  });
  expect(last(seen).status).toBe("ready");

  // `null` has no target to stat, so there is nothing to wait for and the
  // answer is immediate — the initial-state rule (`noPaneTarget`) applied to a
  // hop rather than only to a first mount.
  rerender(null);
  expect(last(seen)).toEqual({ status: "none", src: null, noPane: true });
});

test("the no-pane latch re-arms with the target, so the next paneless hop still enters", async () => {
  const pending = heldFetch();
  const flag = { current: false };
  const seen: boolean[] = [];
  function Probe({ file }: { file: string | null }) {
    const pane = usePaneState({ file, chatOnly: false, noPaneFlag: flag });
    seen.push(pane.noPane);
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe, { file: null }));
  });
  // A null target latches no-pane.
  expect(flag.current).toBe(true);

  // A hop to a real file must clear the latch — left set, the target below
  // would never run the five steps, and worse, a later paneless target would
  // silently skip them too.
  act(() => r.update(createElement(Probe, { file: "/w/one.html" })));
  expect(flag.current).toBe(false);
  await act(async () => {
    pending.find((p) => p.path === "/w/one.html")!.send();
    await Promise.resolve();
  });
  expect(last(seen)).toBe(false);
});
