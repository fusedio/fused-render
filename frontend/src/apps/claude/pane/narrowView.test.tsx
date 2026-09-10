// The narrow view's TWO writes — the one that goes into history and the one
// that deliberately does not — and the toggle's own chrome.
//
// Bugbot #447's no-history rule was about the RESIZE, and it is easy to
// over-apply: a breakpoint crossing is the layout changing under a reader who
// did nothing, so it must not mint a history entry, while a CLICK on the toggle
// moved the reader and Back should undo it (T:8978 writes `paneview` with no
// override, so the store's once-per-visit push applies). Native gets the resize
// half right in the strictest possible way — `crossView` is a ref and never
// reaches the URL at all — which is exactly why a `{history:"replace"}` on the
// click was invisible: nothing else in the file wanted it.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const { useNarrowView } = await import("./useNarrowView");
const { ViewToggle } = await import("./ViewToggle");

type SetCall = [Record<string, string | null | undefined>, unknown];

/** A params store that RECORDS the options it was handed, which is the whole
 *  assertion: `undefined` is a push under the store's own rules, and an
 *  explicit `{history:"replace"}` is not. */
function fakeParams() {
  const values: Record<string, string | undefined> = {};
  const calls: SetCall[] = [];
  const subs = new Set<(all: Record<string, string | undefined>) => void>();
  return {
    calls,
    store: {
      get: (k: string) => values[k],
      set(next: Record<string, string | null | undefined>, opts?: unknown) {
        calls.push([next, opts]);
        for (const [k, v] of Object.entries(next)) {
          if (v === null || v === undefined) delete values[k];
          else values[k] = v;
        }
        for (const cb of subs) cb({ ...values });
      },
      onChange(cb: (all: Record<string, string | undefined>) => void) {
        subs.add(cb);
        return () => subs.delete(cb);
      },
    },
  };
}

const narrowMedia = () =>
  ({
    matches: true,
    addEventListener() {},
    removeEventListener() {},
  }) as unknown as MediaQueryList;

const mounted: ReactTestRenderer[] = [];
afterEach(() => {
  for (const r of mounted.splice(0)) act(() => r.unmount());
});

/** A `.chat-root` stand-in with a settable width and a working
 *  `ResizeObserver` registry — the suite has no DOM, and the observer is the
 *  whole subject. */
function fakeBox(width: number) {
  const subs = new Set<() => void>();
  let w = width;
  const el = {
    getBoundingClientRect: () => ({ width: w }),
  } as unknown as HTMLElement;
  const G = globalThis as Record<string, unknown>;
  const real = G.ResizeObserver;
  G.ResizeObserver = class {
    constructor(private cb: () => void) {}
    observe() {
      subs.add(this.cb);
    }
    disconnect() {
      subs.delete(this.cb);
    }
  };
  installed.push(() => {
    if (real === undefined) delete G.ResizeObserver;
    else G.ResizeObserver = real;
  });
  return {
    ref: { current: el },
    resize(next: number) {
      w = next;
      for (const cb of [...subs]) cb();
    },
  };
}
const installed: Array<() => void> = [];
afterEach(() => {
  for (const undo of installed.splice(0)) undo();
});

function mountWithBox(
  params: ReturnType<typeof fakeParams>["store"],
  box: ReturnType<typeof fakeBox>,
  media?: () => MediaQueryList,
) {
  let api!: ReturnType<typeof useNarrowView>;
  function Probe() {
    api = useNarrowView({
      params: params as never,
      noPane: false,
      boxRef: box.ref,
      // Passing `matchMedia` is what makes a test take the MEDIA road; the
      // box road is the default whenever a ref and an observer are both there.
      ...(media ? { matchMedia: (() => media()) as never } : {}),
    });
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  return { get: () => api };
}

function mountHook(params: ReturnType<typeof fakeParams>["store"]) {
  let api!: ReturnType<typeof useNarrowView>;
  function Probe() {
    api = useNarrowView({
      params: params as never,
      noPane: false,
      matchMedia: (() => narrowMedia()) as never,
    });
    return null;
  }
  let r!: ReactTestRenderer;
  act(() => {
    r = create(createElement(Probe));
  });
  mounted.push(r);
  return { get: () => api };
}

test("the toggle click writes `paneview` with NO history override, so it pushes", () => {
  const p = fakeParams();
  const hook = mountHook(p.store);
  act(() => hook.get().toggle());

  expect(p.calls).toHaveLength(1);
  const [next, opts] = p.calls[0];
  expect(next).toEqual({ paneview: "preview" });
  // The assertion: no second argument at all. `{history:"replace"}` here made
  // Back stop undoing a flip the reader asked for.
  expect(opts).toBeUndefined();
});

test("the toggle reads the view ON SCREEN, not the raw param (T:8946-8956)", () => {
  const p = fakeParams();
  const hook = mountHook(p.store);
  act(() => hook.get().toggle());
  expect(p.calls[0][0]).toEqual({ paneview: "preview" });
  act(() => hook.get().toggle());
  expect(p.calls[1][0]).toEqual({ paneview: "chat" });
  expect(p.calls.every(([, o]) => o === undefined)).toBe(true);
});

test("a breakpoint crossing writes NOTHING to the URL — stricter than a replace", () => {
  // `crossView` is a ref, so the resize half of Bugbot #447 is answered by the
  // param never being written rather than by writing it quietly.
  const p = fakeParams();
  mountHook(p.store);
  expect(p.calls).toHaveLength(0);
});

test("the view toggle carries T's title as well as its destination label", () => {
  // T:4060-4062. The label names WHERE the click goes; the title is the only
  // place that says why the other view matters — that the annotation tools live
  // over there — and a reader who has never armed a mode has no other way in.
  let r!: ReactTestRenderer;
  act(() => {
    r = create(
      createElement(ViewToggle, {
        narrowView: { label: "Comment on preview", toggle: () => {} },
      }),
    );
  });
  mounted.push(r);
  const btn = r.toJSON() as { props: Record<string, unknown> };
  expect(btn.props.title).toBe(
    "Switch between the chat and the preview pane, where the annotation tools are",
  );
  // The single-string aria-label is deliberate (T:8884) and unchanged.
  expect(btn.props["aria-label"]).toBe("Comment on preview");
});

// ---- the breakpoint is about the CHAT'S BOX, not the window (FIX-12) ------

test("a narrow chat box in a wide window IS narrow", () => {
  // Legacy's `@media (max-width: 800px)` was evaluated inside the chat's own
  // iframe, so it answered about the PANEL: a 380px side panel always matched.
  // Native read `window.matchMedia` on the top-level window, so at a 380px
  // panel in a 1280px window not one narrow rule fired — while
  // `pane.css:250-252`'s own comment says the class approach was chosen
  // *because* "the chat can be mounted in a PANE narrower than the window, and
  // a media query would then answer about the wrong box".
  const p = fakeParams();
  const box = fakeBox(380);
  const hook = mountWithBox(p.store, box);
  expect(hook.get().narrow).toBe(true);
  expect(hook.get().classNames).toContain("narrow");
});

test("a wide chat box is not narrow, and a resize of the BOX crosses", () => {
  const p = fakeParams();
  const box = fakeBox(1000);
  const hook = mountWithBox(p.store, box);
  expect(hook.get().narrow).toBe(false);

  act(() => box.resize(500));
  expect(hook.get().narrow).toBe(true);

  // Crossing DOWN with no `paneview` set keeps the preview the reader was just
  // looking at on screen — the same rule the media road has, and still with no
  // param write, because a resize is not a navigation (Bugbot PR #447).
  expect(hook.get().view).toBe("preview");
  expect(p.calls).toHaveLength(0);

  act(() => box.resize(1200));
  expect(hook.get().narrow).toBe(false);
});

test("exactly 800 is narrow; 801 is not", () => {
  const p = fakeParams();
  const box = fakeBox(800);
  expect(mountWithBox(p.store, box).get().narrow).toBe(true);
  expect(mountWithBox(fakeParams().store, fakeBox(801)).get().narrow).toBe(false);
});

test("a box not laid out yet is not 'narrow' — zero is not a width", () => {
  // A zero width during a mount would otherwise collapse the layout for a frame
  // and then uncollapse it.
  const box = fakeBox(0);
  const hook = mountWithBox(fakeParams().store, box);
  expect(hook.get().narrow).toBe(false);
  act(() => box.resize(400));
  expect(hook.get().narrow).toBe(true);
});

test("no ResizeObserver: it falls back to the window query", () => {
  const G = globalThis as Record<string, unknown>;
  const real = G.ResizeObserver;
  delete G.ResizeObserver;
  try {
    // A WIDE box, so a live observer would say "not narrow" — the media stub
    // says narrow, and that is what must win when there is no observer.
    const hook = mountWithBox(fakeParams().store, fakeBox(1200), narrowMedia);
    expect(hook.get().narrow).toBe(true);
  } finally {
    if (real === undefined) delete G.ResizeObserver;
    else G.ResizeObserver = real;
  }
});
