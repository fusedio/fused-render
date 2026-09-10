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
