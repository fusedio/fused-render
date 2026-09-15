// THE TWO THINGS THAT ONLY BREAK WHEN THE FLAG ARRIVES LATE.
//
// `task_peek_enabled` is read asynchronously, so a page paints once with the
// feature off and again with it on. Both bugs below are invisible in either
// steady state and appear only on that flip — which is every real load.
//
//   * A HOOK BEHIND THE FLAG. `usePeekHost()` starts false and turns true in a
//     layout effect; a `usePeekedKey()` call behind that condition is a hook
//     that APPEARS between two renders, and React throws outright.
//   * A STALE ROUTE MARK. `useNavEpoch`'s ignore list gains `peek` when the
//     pref lands, but the mark it compares a Back against was recorded before
//     that — so a traversal that only dropped `?peek=` looked like a different
//     route and remounted the page.
//
// Both are executed here rather than grepped: the failure is a render, and a
// source test cannot tell a conditional hook from a conditional value.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { readFileSync } from "node:fs";
import { createElement } from "react";
import { act, create } from "react-test-renderer";

const store = await import("./task-peek-store");
const { TaskList } = await import("./ScheduleTaskViews");
const { useNavEpoch } = await import("@platform/lib/hooks");
const { NAV_EVENT } = await import("@platform/lib/router");

const win = globalThis.window as unknown as {
  addEventListener: (t: string, f: () => void) => void;
  removeEventListener: (t: string, f: () => void) => void;
  dispatchEvent?: (e: unknown) => void;
};
const loc = globalThis.location as unknown as { pathname: string; search: string };

/** The shim's window has no real event target; give it one so the hooks under
 *  test can actually be woken. */
const handlers = new Map<string, Set<() => void>>();
const realAdd = win.addEventListener;
const realRemove = win.removeEventListener;

beforeEach(() => {
  store.resetPeekStoreForTests();
  handlers.clear();
  win.addEventListener = (type: string, fn: () => void) => {
    if (!handlers.has(type)) handlers.set(type, new Set());
    handlers.get(type)!.add(fn);
  };
  win.removeEventListener = (type: string, fn: () => void) => {
    handlers.get(type)?.delete(fn);
  };
});

afterEach(() => {
  win.addEventListener = realAdd;
  win.removeEventListener = realRemove;
});

function fire(type: string) {
  for (const fn of [...(handlers.get(type) ?? [])]) fn();
}

describe("a view that paints before the flag lands", () => {
  it("survives the host turning on — no hook appears between two renders", async () => {
    // THE BUG THIS PINS: `peekOn ? usePeekedKey() : null`. The first commit runs
    // one hook, the second runs two, and React throws "rendered more hooks than
    // during the previous render" — on every load of the page with the feature
    // on, because `host` is always false for the first paint.
    let tree: ReturnType<typeof create> | undefined;
    await act(async () => {
      tree = create(createElement(TaskList, { tasks: [] }));
    });
    expect(store.getPeekState().host).toBe(false);

    // …and now the page announces itself, exactly as `useTaskPeekHost` does in
    // its layout effect once the pref has answered.
    await act(async () => {
      store.setPeekHost(true);
    });
    expect(store.getPeekState().host).toBe(true);

    // Opening a task re-renders every row with the halo; still one shape.
    await act(async () => {
      store.openPeek("sess-1");
    });
    expect(store.getPeekState().key).toBe("sess-1");

    // …and back off again, which is what leaving the page does.
    await act(async () => {
      store.setPeekHost(false);
    });
    await act(async () => {
      tree?.unmount();
    });
  });
});

describe("useNavEpoch when the ignore list arrives late", () => {
  function Epoch({ ignore }: { ignore: readonly string[] }) {
    const epoch = useNavEpoch(ignore);
    return createElement("span", null, String(epoch));
  }
  const shown = (tree: ReturnType<typeof create>) =>
    (tree.toJSON() as { children: string[] }).children[0];

  it("re-reads the mark when `peek` joins the list, and does not bump for it", async () => {
    loc.pathname = "/tasks";
    loc.search = "?view=list&peek=sess-1";
    let tree: ReturnType<typeof create> | undefined;
    // First paint: the pref has not answered, so nothing is ignored and the
    // mark carries `?peek=`.
    await act(async () => {
      tree = create(createElement(Epoch, { ignore: [] }));
    });
    expect(shown(tree!)).toBe("0");

    // The pref lands. Re-reading the mark is a CORRECTION, not a navigation.
    await act(async () => {
      tree?.update(createElement(Epoch, { ignore: ["peek"] }));
    });
    expect(shown(tree!)).toBe("0");

    // Back, dropping only the peek. Same route — so no remount.
    await act(async () => {
      loc.search = "?view=list";
      fire("popstate");
    });
    expect(shown(tree!)).toBe("0");

    await act(async () => {
      tree?.unmount();
    });
  });

  it("still bumps for a traversal that lands somewhere else", async () => {
    loc.pathname = "/tasks";
    loc.search = "?view=list&peek=sess-1";
    let tree: ReturnType<typeof create> | undefined;
    await act(async () => {
      tree = create(createElement(Epoch, { ignore: [] }));
    });
    await act(async () => {
      tree?.update(createElement(Epoch, { ignore: ["peek"] }));
    });
    // A different view is a different route, peek or no peek.
    await act(async () => {
      loc.search = "?view=board";
      fire("popstate");
    });
    expect(shown(tree!)).toBe("1");
    // …and so is a different path.
    await act(async () => {
      loc.pathname = "/apps";
      fire("popstate");
    });
    expect(shown(tree!)).toBe("2");
    await act(async () => {
      tree?.unmount();
    });
  });

  it("bumps for an explicit navigation whatever is ignored", async () => {
    loc.pathname = "/tasks";
    loc.search = "";
    let tree: ReturnType<typeof create> | undefined;
    await act(async () => {
      tree = create(createElement(Epoch, { ignore: ["peek"] }));
    });
    await act(async () => {
      fire(NAV_EVENT);
    });
    // NAV_EVENT is a navigate()/navigateUrl(), which every route has always
    // remounted on — including the same-path ones. Only the traversal is
    // narrowed.
    expect(shown(tree!)).toBe("1");
    await act(async () => {
      tree?.unmount();
    });
  });
});

describe("the peek's Escape rule", () => {
  // The blur-first rule lives in one place so BOTH routes into it agree: the
  // panel's own document listener, and the native chat's `onEscape`, which
  // hands the press up before that listener ever sees it (Bugbot, PR #1133).
  const SRC = readFileSync(new URL("./TaskPeek.tsx", import.meta.url).pathname, "utf8");

  it("is one function, spent by the native hand-up as well as the listener", () => {
    expect(SRC).toContain("const escapeOrBlur = useCallback((doc: Document) => {");
    // The native chat's hand-up goes through it…
    expect(SRC).toContain("onEscape={() => escapeOrBlur(document)}");
    // …and so do both keydown listeners, through `peekKey`, each with the
    // document the press actually happened in (this page's, and the app
    // preview frame's).
    expect(SRC).toContain("        escapeOrBlur(doc);");
    expect(SRC).toContain("peekKey(e, document);");
    expect(SRC).toContain("peekKey(e, (e.target as Node | null)?.ownerDocument ?? document);");
    // Nothing closes on Escape without asking first.
    expect(SRC).not.toContain("onEscape={() => closePeek()}");
  });

  it("blurs a composer with words in it before it closes anything", () => {
    expect(SRC).toContain("if (typing) {");
    expect(SRC).toContain("el.blur();");
  });

  it("keeps its own identity stable, so the listener is not re-bound per render", () => {
    // No deps: it reads `document.activeElement` at PRESS time, which is the
    // only thing that can have changed between one render and the next.
    const at = SRC.indexOf("const escapeOrBlur = useCallback(");
    expect(SRC.slice(at, at + 500)).toContain("}, []);");
  });
});
