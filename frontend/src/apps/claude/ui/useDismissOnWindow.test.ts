// The two dismissals Base UI's outside-press cannot reach (T:12146/12149,
// T:12602/12605) — bound only while the popover is open, and unbound with it.
//
// The listener BOOKKEEPING is the point of most of these: a `resize` handler
// left registered by a closed popover fires on every frame of a window drag,
// and this hook is about to be shared by three popovers, so a leak here leaks
// three times.
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();

const { useDismissOnWindow } = await import("./useDismissOnWindow");

type Handler = () => void;

/** A window stand-in that RECORDS its registrations, so a test can assert what
 *  is bound as well as what fires. */
function fakeWindow() {
  const bound = new Map<string, Set<Handler>>();
  return {
    view: {
      addEventListener(type: string, fn: Handler) {
        if (!bound.has(type)) bound.set(type, new Set());
        bound.get(type)!.add(fn);
      },
      removeEventListener(type: string, fn: Handler) {
        bound.get(type)?.delete(fn);
      },
    } as unknown as Window,
    count: (type: string) => bound.get(type)?.size ?? 0,
    fire(type: string) {
      for (const fn of [...(bound.get(type) ?? [])]) fn();
    },
  };
}

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

function mount(open: boolean, close: Handler, opts?: Record<string, unknown>) {
  const w = fakeWindow();
  function Probe({ isOpen }: { isOpen: boolean }) {
    useDismissOnWindow(isOpen, close, { target: w.view, ...opts });
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe, { isOpen: open }));
  });
  mounted.push(r);
  return {
    w,
    setOpen(next: boolean) {
      act(() => r.update(createElement(Probe, { isOpen: next })));
    },
  };
}

test("closed: nothing is bound at all", () => {
  const { w } = mount(false, () => {});
  expect(w.count("blur")).toBe(0);
  expect(w.count("resize")).toBe(0);
});

test("open: both events are bound, and either one closes", () => {
  let closes = 0;
  const a = mount(true, () => {
    closes += 1;
  });
  expect(a.w.count("blur")).toBe(1);
  expect(a.w.count("resize")).toBe(1);

  // `blur` is the one outside-press cannot see: the click landed inside the
  // preview iframe, so it never reached this document (T:12602).
  a.w.fire("blur");
  expect(closes).toBe(1);

  // `resize` is T:12605's — "nothing repositions an open menu […] so it goes
  // away instead" — and here it matters twice over, because the pill's own
  // width moves on the fit ladder.
  a.w.fire("resize");
  expect(closes).toBe(2);
});

test("closing UNBINDS both, so a closed popover does not wake on a window drag", () => {
  let closes = 0;
  const h = mount(true, () => {
    closes += 1;
  });
  h.setOpen(false);
  expect(h.w.count("blur")).toBe(0);
  expect(h.w.count("resize")).toBe(0);
  h.w.fire("blur");
  h.w.fire("resize");
  expect(closes).toBe(0);
});

test("unmounting while open unbinds too", () => {
  const h = mount(true, () => {});
  expect(h.w.count("blur")).toBe(1);
  for (const r of mounted.splice(0)) act(() => r.unmount());
  expect(h.w.count("blur")).toBe(0);
  expect(h.w.count("resize")).toBe(0);
});

test("`resize: false` binds only blur — for a popover that really does follow its anchor", () => {
  const h = mount(true, () => {}, { resize: false });
  expect(h.w.count("blur")).toBe(1);
  expect(h.w.count("resize")).toBe(0);
});

test("re-opening binds a fresh pair rather than stacking", () => {
  const h = mount(true, () => {});
  h.setOpen(false);
  h.setOpen(true);
  expect(h.w.count("blur")).toBe(1);
  expect(h.w.count("resize")).toBe(1);
});

test("no window at all: a no-op, not a throw", () => {
  function Probe() {
    useDismissOnWindow(true, () => {}, { target: null });
    return null;
  }
  expect(() => {
    act(() => {
      mounted.push(create(createElement(Probe)));
    });
  }).not.toThrow();
});
