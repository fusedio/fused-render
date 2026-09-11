// The bug this guards: the search box's placeholder hint picked its long
// form off a `boxWide` flag that was measured once, in a `useLayoutEffect`
// with `[]` deps, off an object ref. The box the ref pointed at is portaled
// into the crumb bar once a folder claims it (search-slot.ts) — a swap that
// rebuilds the node — and the one-shot effect never re-attached its
// ResizeObserver to the replacement, so `boxWide` froze at whatever the
// FIRST (soon-to-be-discarded) node measured. A field rendered at 860px
// wide kept showing the short placeholder forever.
//
// `useWidthThresholdRef` fixes this with a callback ref, which React calls
// on every mount AND every unmount — portal swaps included — so there is no
// scenario here to fake a real DOM node for: calling the returned function
// directly, the way React would on each (re)attach, is the whole test.
import { expect, test } from "bun:test";
import { renderHook } from "@apps/explorer/listing/hook-harness";
import { useWidthThresholdRef } from "@apps/explorer/listing/search-hint-width";

/** A fake ResizeObserver that just remembers what it is observing, so a test
 * can drive a resize by hand without a real layout engine. */
class FakeResizeObserver {
  static instances: FakeResizeObserver[] = [];
  cb: () => void;
  observed: { clientWidth: number } | null = null;
  disconnected = false;
  constructor(cb: () => void) {
    this.cb = cb;
    FakeResizeObserver.instances.push(this);
  }
  observe(el: { clientWidth: number }) {
    this.observed = el;
  }
  disconnect() {
    this.disconnected = true;
  }
  fire() {
    this.cb();
  }
}

function withFakeResizeObserver<T>(fn: () => T): T {
  const original = (globalThis as Record<string, unknown>).ResizeObserver;
  FakeResizeObserver.instances = [];
  (globalThis as Record<string, unknown>).ResizeObserver = FakeResizeObserver;
  try {
    return fn();
  } finally {
    (globalThis as Record<string, unknown>).ResizeObserver = original;
  }
}

function fakeEl(clientWidth: number): HTMLElement {
  return { clientWidth } as unknown as HTMLElement;
}

test("attaching to a wide element reports wide immediately, no resize needed", () => {
  withFakeResizeObserver(() => {
    const seen: boolean[] = [];
    const box = renderHook(() => useWidthThresholdRef(340, (w) => seen.push(w)));
    box.current()(fakeEl(860));
    expect(seen).toEqual([true]);
    box.unmount();
  });
});

test("attaching to a narrow element reports narrow", () => {
  withFakeResizeObserver(() => {
    const seen: boolean[] = [];
    const box = renderHook(() => useWidthThresholdRef(340, (w) => seen.push(w)));
    box.current()(fakeEl(200));
    expect(seen).toEqual([false]);
    box.unmount();
  });
});

test("a resize on the SAME element re-measures", () => {
  withFakeResizeObserver(() => {
    const seen: boolean[] = [];
    const box = renderHook(() => useWidthThresholdRef(340, (w) => seen.push(w)));
    const el = fakeEl(200);
    box.current()(el);
    (el as unknown as { clientWidth: number }).clientWidth = 860;
    FakeResizeObserver.instances[0].fire();
    expect(seen).toEqual([false, true]);
    box.unmount();
  });
});

// The regression test: a node swap — unmount (null) then a fresh element —
// is exactly what a portal relocation does to the ref. The old bug's
// one-shot effect would have stopped reporting anything after the first
// node; this must keep reporting off whichever node is live.
test("a node swap (portal relocation) re-attaches and re-measures the NEW node", () => {
  withFakeResizeObserver(() => {
    const seen: boolean[] = [];
    const box = renderHook(() => useWidthThresholdRef(340, (w) => seen.push(w)));
    const ref = box.current();

    // First mount: a narrow inline node, before the bar claims the slot.
    ref(fakeEl(200));
    expect(FakeResizeObserver.instances).toHaveLength(1);
    expect(seen).toEqual([false]);

    // The portal swap: React tears the old node down (ref called with null)
    // and hands the callback a fresh node inside the bar, already at full
    // width — the observer on the OLD node must not be left dangling.
    ref(null);
    expect(FakeResizeObserver.instances[0].disconnected).toBe(true);

    ref(fakeEl(860));
    expect(FakeResizeObserver.instances).toHaveLength(2);
    expect(seen).toEqual([false, true]);

    box.unmount();
  });
});
