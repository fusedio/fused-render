// The param boundary's COUNT — behaviour, not a grep.
//
// The flag is one bit on `window`, and two surfaces on the Tasks page claim it
// at once: the Cards wall and the side peek. Set-and-delete gave whichever
// unmounted first the power to unmark the window under the other, and the
// survivor's framed chat went back to climbing to `/tasks` for its `session_id`
// — which is the "chat template home screen instead of the conversation" bug
// this file exists to keep fixed.
//
// What is worth executing is the arithmetic of the count: two claims in either
// release order, and a release that is spent twice.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { beforeEach, describe, expect, it } from "bun:test";

const { claimParamBoundary, resetParamBoundaryForTests } = await import("./param-boundary");

/** The flag as the framed runtime reads it (static/runtime.js `findTarget`). */
function marked(): boolean {
  return (globalThis as { window: { _fusedParamBoundary?: boolean } }).window
    ._fusedParamBoundary === true;
}

beforeEach(() => {
  resetParamBoundaryForTests();
});

describe("claimParamBoundary", () => {
  it("marks the window for the first claim and clears it on the last release", () => {
    expect(marked()).toBe(false);
    const release = claimParamBoundary();
    expect(marked()).toBe(true);
    release();
    expect(marked()).toBe(false);
  });

  it("holds the flag while a SECOND claim is outstanding — released in order", () => {
    const cards = claimParamBoundary();
    const peek = claimParamBoundary();
    cards();
    // The wall has gone (the reader switched to List) and the peek is still up:
    // its frame must keep reading its own `src`.
    expect(marked()).toBe(true);
    peek();
    expect(marked()).toBe(false);
  });

  it("…and released in the OTHER order", () => {
    const cards = claimParamBoundary();
    const peek = claimParamBoundary();
    peek();
    expect(marked()).toBe(true);
    cards();
    expect(marked()).toBe(false);
  });

  it("treats a second release as the no-op it is", () => {
    // React can run a cleanup more than once (StrictMode's double-invoke, an
    // effect re-running), and a release that decremented twice would unmark the
    // window while a live frame was still holding it.
    const cards = claimParamBoundary();
    const peek = claimParamBoundary();
    cards();
    cards();
    cards();
    expect(marked()).toBe(true);
    peek();
    expect(marked()).toBe(false);
  });

  it("never leaves the count below zero", () => {
    const one = claimParamBoundary();
    one();
    one();
    // A fresh claim after an over-release must still mark the window: a count
    // stuck at -1 would need two claims before the flag came back.
    const two = claimParamBoundary();
    expect(marked()).toBe(true);
    two();
    expect(marked()).toBe(false);
  });
});

describe("useParamBoundary", () => {
  // The hook is `useEffect(() => active ? claim() : undefined, [active])`, so
  // what is worth checking is that the TRI-STATE caller's "not yet" holds
  // nothing: the native-chat flag reads `null` before it reads `false`, and a
  // `null` treated as truthy marked the window for one paint and unmarked it
  // the next — a claim about the window that was never true.
  it("claims only while active, and gives it back when it stops", async () => {
    const { useParamBoundary } = await import("./param-boundary");
    const { act, create } = await import("react-test-renderer");
    const { createElement } = await import("react");

    function Host({ active }: { active: boolean }) {
      useParamBoundary(active);
      return null;
    }

    let tree: ReturnType<typeof create> | undefined;
    await act(async () => {
      tree = create(createElement(Host, { active: false }));
    });
    expect(marked()).toBe(false);

    await act(async () => {
      tree?.update(createElement(Host, { active: true }));
    });
    expect(marked()).toBe(true);

    await act(async () => {
      tree?.update(createElement(Host, { active: false }));
    });
    expect(marked()).toBe(false);

    // …and a mount that is active from the start, unmounted.
    await act(async () => {
      tree?.update(createElement(Host, { active: true }));
    });
    expect(marked()).toBe(true);
    await act(async () => {
      tree?.unmount();
    });
    expect(marked()).toBe(false);
  });

  it("lets two hosts overlap without either unmarking the other", async () => {
    const { useParamBoundary } = await import("./param-boundary");
    const { act, create } = await import("react-test-renderer");
    const { createElement, Fragment } = await import("react");

    function Host({ active }: { active: boolean }) {
      useParamBoundary(active);
      return null;
    }
    function Both({ wall, peek }: { wall: boolean; peek: boolean }) {
      return createElement(
        Fragment,
        null,
        createElement(Host, { active: wall, key: "wall" }),
        createElement(Host, { active: peek, key: "peek" }),
      );
    }

    let tree: ReturnType<typeof create> | undefined;
    await act(async () => {
      tree = create(createElement(Both, { wall: true, peek: true }));
    });
    expect(marked()).toBe(true);
    // The Cards wall unmounts (List → Board) with the peek still open.
    await act(async () => {
      tree?.update(createElement(Both, { wall: false, peek: true }));
    });
    expect(marked()).toBe(true);
    await act(async () => {
      tree?.update(createElement(Both, { wall: false, peek: false }));
    });
    expect(marked()).toBe(false);
  });
});
