// terminalDockStore.ts's open/close store already gets chip-level coverage
// through TerminalDock.test.tsx (click toggles, hover does nothing). This
// file covers the pending-request slot added for the "open the drawer in a
// folder / run a command in it" flow (openTerminal/peekPendingTerminalRequest/
// takePendingTerminalRequest/usePendingTerminalRequestVersion) directly,
// against the plain exported functions — no React tree needed for any of
// this except `usePendingTerminalRequestVersion`, which — like
// `useTerminalDockOpen` in TerminalDock.test.tsx — needs a mounted component
// to observe re-renders through.
import { afterEach, describe, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import {
  canRunInTerminal,
  closeTerminalDock,
  openTerminal,
  peekPendingTerminalRequest,
  registerTerminalDrawerMounted,
  resetTerminalDockForTests,
  takePendingTerminalRequest,
  useCanRunInTerminal,
  usePendingTerminalRequestVersion,
} from "./terminalDockStore";
import { IS_EMBED } from "./router";
import { isWindows } from "./platform";

let renderers: ReactTestRenderer[] = [];
function renderTracked(node: Parameters<typeof create>[0]): ReactTestRenderer {
  const renderer = create(node);
  renderers.push(renderer);
  return renderer;
}

afterEach(() => {
  for (const renderer of renderers) renderer.unmount();
  renderers = [];
  resetTerminalDockForTests();
});

describe("openTerminal", () => {
  test("a bare call with no request leaves the pending slot untouched", () => {
    closeTerminalDock();
    openTerminal();
    expect(peekPendingTerminalRequest()).toBeNull();
  });

  test("records a cwd+command request for later consumption", () => {
    openTerminal({ cwd: "/tmp/foo", command: "ls" });
    expect(peekPendingTerminalRequest()).toEqual({ cwd: "/tmp/foo", command: "ls" });
  });

  test("a newer request replaces an unconsumed older one (one slot, not a queue)", () => {
    openTerminal({ cwd: "/tmp/foo" });
    openTerminal({ cwd: "/tmp/bar" });
    expect(peekPendingTerminalRequest()).toEqual({ cwd: "/tmp/bar" });
  });

  test("peek does not consume; take does, and only once", () => {
    openTerminal({ command: "ls" });
    expect(peekPendingTerminalRequest()).toEqual({ command: "ls" });
    expect(peekPendingTerminalRequest()).toEqual({ command: "ls" });
    expect(takePendingTerminalRequest()).toEqual({ command: "ls" });
    expect(takePendingTerminalRequest()).toBeNull();
    expect(peekPendingTerminalRequest()).toBeNull();
  });

  test("a request with neither field set does not touch the pending slot", () => {
    openTerminal({ cwd: "/tmp/foo" });
    openTerminal({});
    expect(peekPendingTerminalRequest()).toEqual({ cwd: "/tmp/foo" });
  });
});

function VersionProbe({ onRender }: { onRender: (v: number) => void }) {
  const version = usePendingTerminalRequestVersion();
  onRender(version);
  return null;
}

describe("usePendingTerminalRequestVersion", () => {
  test("bumps on every recorded request, even a repeat while already open", async () => {
    const seen: number[] = [];
    renderTracked(<VersionProbe onRender={(v) => seen.push(v)} />);
    expect(seen).toEqual([0]);

    // Same async-act shape TerminalDrawer.test.tsx's `fireKeyDown` uses for
    // any `useSyncExternalStore` notification: the listener notification
    // resolves on a microtask past a synchronous `act(() => {...})`'s
    // return, so a plain sync `act` misses the re-render.
    await act(async () => {
      openTerminal({ command: "one" });
      await Promise.resolve();
    });
    expect(seen).toEqual([0, 1]);

    // Drawer already open (`open` doesn't change) — the version still bumps
    // so a mounted TerminalDrawer's consuming effect re-runs.
    await act(async () => {
      openTerminal({ command: "two" });
      await Promise.resolve();
    });
    expect(seen).toEqual([0, 1, 2]);
  });

  test("does not bump for a request with neither field set", async () => {
    const seen: number[] = [];
    renderTracked(<VersionProbe onRender={(v) => seen.push(v)} />);

    await act(async () => {
      openTerminal({});
      await Promise.resolve();
    });
    expect(seen).toEqual([0]);
  });
});

describe("canRunInTerminal", () => {
  test("is false before any TerminalDrawer registers as mounted", () => {
    // No drawer mounted (the onboarding route, or a test that renders
    // nothing) means no Run affordance, whatever !IS_EMBED && !isWindows says.
    expect(canRunInTerminal()).toBe(false);
  });

  test("matches the drawer's own !IS_EMBED && !isWindows gate once mounted", () => {
    // App.tsx gates both `TerminalDrawer` and the `TerminalDock` chip on this
    // exact pair, so every Run affordance elsewhere in the app must agree
    // with it rather than re-derive its own version of the check.
    const unregister = registerTerminalDrawerMounted();
    expect(canRunInTerminal()).toBe(!IS_EMBED && !isWindows);
    unregister();
  });
});

describe("useCanRunInTerminal", () => {
  test("re-renders as the drawer mounts and unmounts", async () => {
    const seen: boolean[] = [];
    function Probe() {
      seen.push(useCanRunInTerminal());
      return null;
    }
    renderTracked(<Probe />);
    expect(seen).toEqual([false]);

    let unregister!: () => void;
    await act(async () => {
      unregister = registerTerminalDrawerMounted();
      await Promise.resolve();
    });
    expect(seen).toEqual([false, !IS_EMBED && !isWindows]);

    await act(async () => {
      unregister();
      await Promise.resolve();
    });
    expect(seen).toEqual([false, !IS_EMBED && !isWindows, false]);
  });
});
