// useAppPageGitPeekWidth.ts — the git peek's drag-to-resize state.
//
// Two behavioural findings pinned here, both confirmed against the hook's own
// code before this file existed:
//
//   3. `onSeamPointerDown` attaches `pointermove`/`pointerup`/`pointercancel`
//      to `window`; the only removal path was inside `onSeamPointerUp`. A
//      component that unmounts mid-drag (a nav, an app-folder key change, a
//      route change while the seam button is still down) left the three
//      listeners attached for the life of the document, each one calling
//      `setChosen`/`setDragging` on a dead hook.
//   4. The seam's `pointerdown` did neither `preventDefault()` nor
//      `setPointerCapture()`, so dragging it swept a native text selection
//      across the page instead of just resizing the panel.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement, createRef, type ReactElement } from "react";
const { useAppPageGitPeekWidth } = await import("@shell/useAppPageGitPeekWidth");

/** Counts net listener attachment on `window`, keyed by event name — an
 *  unmount that cleans up properly nets to zero for every drag event.
 *  `installDomShim`'s `window` is a plain stand-in object (not a real
 *  `EventTarget`), so patching its two methods directly is enough — no
 *  binding needed since neither stub reads `this`. */
function watchWindowListeners(): {
  netFor: (type: string) => number;
  restore: () => void;
} {
  const w = window as unknown as {
    addEventListener: (type: string, ...rest: unknown[]) => void;
    removeEventListener: (type: string, ...rest: unknown[]) => void;
  };
  const counts = new Map<string, number>();
  const realAdd = w.addEventListener;
  const realRemove = w.removeEventListener;
  w.addEventListener = (type, ...rest) => {
    counts.set(type, (counts.get(type) ?? 0) + 1);
    return realAdd.call(w, type, ...rest);
  };
  w.removeEventListener = (type, ...rest) => {
    counts.set(type, (counts.get(type) ?? 0) - 1);
    return realRemove.call(w, type, ...rest);
  };
  return {
    netFor: (type) => counts.get(type) ?? 0,
    restore: () => {
      w.addEventListener = realAdd;
      w.removeEventListener = realRemove;
    },
  };
}

// The hook's mount effect observes `splitRef` with a real `ResizeObserver`,
// which bun's DOM-less test environment does not provide — inert, the same
// stand-in TaskPeekFrame.test.tsx installs for the same reason.
(globalThis as Record<string, unknown>).ResizeObserver ??= class {
  observe() {}
  disconnect() {}
};

// React schedules a passive-effect cleanup (an unmounted component's own
// `useEffect` teardown, here included) for a LATER flush rather than running
// it inside the `act()` call that triggered it — under bun's test renderer
// that flush lands on the NEXT `act()` call to run at all, which would
// otherwise leak a PRIOR test's listener teardown into this test's own
// count. Draining it explicitly, before installing the watch below, is what
// keeps each test's counts its own.
async function flush(): Promise<void> {
  await act(async () => {
    // React's scheduler here uses a macrotask (`MessageChannel`/
    // `setImmediate`-equivalent) to run a passive-effect flush it did not run
    // synchronously — a microtask-only wait (bare `Promise.resolve()` chains)
    // never reaches it, so this needs an actual event-loop tick.
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function mountProbe(): {
  seamPointerDown: (capture: { called: boolean }) => void;
  unmount: () => Promise<void>;
} {
  const splitRef = createRef<HTMLElement>();
  // `useAppPageGitPeekWidth` only reads `.current` — a plain object with the
  // handful of members the hook's effects touch is enough, no real DOM.
  (splitRef as { current: unknown }).current = {
    clientWidth: 1200,
  } as unknown as HTMLElement;

  let down: ((e: unknown) => void) | undefined;
  let renderer!: ReactTestRenderer;
  function Probe(): ReactElement | null {
    const layout = useAppPageGitPeekWidth(splitRef);
    down = layout.onSeamPointerDown as unknown as (e: unknown) => void;
    return null;
  }
  act(() => {
    renderer = create(createElement(Probe));
  });
  return {
    seamPointerDown: (capture) => {
      act(() => {
        down!({
          clientX: 100,
          pointerId: 1,
          preventDefault: () => {
            capture.called = true;
          },
          currentTarget: { setPointerCapture: () => {} },
        });
      });
    },
    // `act`'s SYNC overload does not wait for passive effects that React
    // schedules for a later flush — `unmount()`'s own `useEffect` cleanups
    // are exactly that kind, so a caller that needs the cleanup to have
    // actually RUN (not merely be queued) awaits the async overload instead.
    unmount: async () => {
      await act(async () => {
        renderer.unmount();
      });
    },
  };
}

test("a seam press claims the gesture — preventDefault, so dragging never text-selects the page", async () => {
  const probe = mountProbe();
  const capture = { called: false };
  probe.seamPointerDown(capture);
  expect(capture.called).toBe(true);
  await probe.unmount();
  await flush();
});

test("unmounting mid-drag removes all three window listeners it attached", async () => {
  await flush(); // drain any earlier test's still-pending passive-effect teardown first
  // Mounted BEFORE the watch is installed: React can flush an earlier test's
  // deferred passive-effect cleanup as a side effect of committing this new
  // tree, and that flush must not be mistaken for something THIS drag did.
  const probe = mountProbe();
  const watch = watchWindowListeners();
  try {
    probe.seamPointerDown({ called: false });
    expect(watch.netFor("pointermove")).toBe(1);
    expect(watch.netFor("pointerup")).toBe(1);
    expect(watch.netFor("pointercancel")).toBe(1);
    await probe.unmount();
    await flush();
    expect(watch.netFor("pointermove")).toBe(0);
    expect(watch.netFor("pointerup")).toBe(0);
    expect(watch.netFor("pointercancel")).toBe(0);
  } finally {
    watch.restore();
  }
});
