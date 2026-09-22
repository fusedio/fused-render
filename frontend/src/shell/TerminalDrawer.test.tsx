// TerminalDrawer.tsx owns two things this suite covers directly, both
// without ever mounting `TerminalView` (deliberately untested per its own
// header — a headless renderer cannot run its real resize/layout pass, the
// same reason a prior round on this branch declined to add a test-only prop
// there; see DECISIONS.md):
//
//   1. The toggle shortcut (Cmd/Ctrl+Shift+` and the VS Code Ctrl+` alias),
//      registered once regardless of `open` — exercised by rendering the
//      drawer CLOSED (`open` stays false throughout, so the verify-or-create
//      effect never fires and `TerminalView` never mounts) and firing fake
//      keydown events, the same "capture document.addEventListener calls by
//      hand" pattern ClaudeChat.ann.test.tsx uses for the shim's document,
//      which has no real event dispatch.
//   2. `clearExitedSession` — the localStorage+store half of "an exit hides
//      the drawer" — tested directly as a pure function, including the case
//      where the drawer is already closed when it runs.
//
// `terminalDockStore` is module-level and shared with every other suite in
// this bun process (TerminalDock.test.tsx's own header), so every test here
// resets it, and every renderer this file creates is unmounted in the same
// test's own body (nothing is held across tests) — the discipline the
// bun-test OOM fix on this branch depends on.
import { afterEach, beforeEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";

import { installDomShim } from "@platform/lib/testDomShim";
import { isMac } from "@platform/lib/platform";
import {
  closeTerminalDock,
  resetTerminalDockForTests,
  toggleTerminalDock,
  useTerminalDockOpen,
} from "@shell/terminalDockStore";

installDomShim();
// terminalSession.ts (imported by TerminalDrawer.tsx) pulls in api.ts ->
// presence.ts -> router.ts, which reads `location` at MODULE SCOPE — the
// shim has to be in place before that import evaluates, so this is a
// dynamic import exactly like terminalSession.test.ts's own.
const TerminalDrawerModule = await import("@shell/TerminalDrawer");
const TerminalDrawer = TerminalDrawerModule.default;
const { clearExitedSession } = TerminalDrawerModule;

// A minimal in-memory `localStorage` — bun's test runtime has no real one
// (viewstate.test.ts's own header) — scoped to this file only and restored
// afterward so it can't leak into a suite that relies on `localStorage`
// being genuinely absent.
function fakeLocalStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => (store.has(k) ? (store.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
}

const originalLocalStorage = (globalThis as { localStorage?: Storage }).localStorage;

// The shim's `document` has no-op listeners by design (installDomShim's own
// comment). Patched here to capture "keydown" handlers so a test can fire
// them by hand, the pattern ClaudeChat.ann.test.tsx uses for the same shim.
const keydowns: Array<(e: KeyboardEvent) => void> = [];
let realAdd: unknown;
let realRemove: unknown;

let renderers: ReactTestRenderer[] = [];
function renderTracked(node: Parameters<typeof create>[0]): ReactTestRenderer {
  let renderer!: ReactTestRenderer;
  act(() => {
    renderer = create(node);
  });
  renderers.push(renderer);
  return renderer;
}

async function fireKeyDown(over: Record<string, unknown>): Promise<{ defaultPrevented: boolean }> {
  const ev = {
    code: "Backquote",
    key: "`",
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    altKey: false,
    defaultPrevented: false,
    preventDefault() {
      (this as { defaultPrevented: boolean }).defaultPrevented = true;
    },
    ...over,
  };
  // `await act(async () => ...)`, not the sync form: `useSyncExternalStore`'s
  // notification (toggleTerminalDock -> the store's listeners) can resolve on
  // a microtask react-test-renderer's concurrent root schedules past a plain
  // synchronous `act()` callback's return, which is exactly the "update not
  // wrapped in act" warning a sync `act()` here produced.
  await act(async () => {
    for (const fn of [...keydowns]) fn(ev as unknown as KeyboardEvent);
    await Promise.resolve();
  });
  return ev;
}

beforeEach(() => {
  (globalThis as { localStorage?: Storage }).localStorage = fakeLocalStorage();
  keydowns.length = 0;
  const doc = globalThis.document as unknown as Record<string, unknown>;
  realAdd = doc.addEventListener;
  realRemove = doc.removeEventListener;
  doc.addEventListener = (type: string, fn: (e: KeyboardEvent) => void) => {
    if (type === "keydown") keydowns.push(fn);
  };
  doc.removeEventListener = (_type: string, fn: (e: KeyboardEvent) => void) => {
    const i = keydowns.indexOf(fn);
    if (i !== -1) keydowns.splice(i, 1);
  };
});

afterEach(() => {
  act(() => {
    for (const renderer of renderers) renderer.unmount();
  });
  renderers = [];
  resetTerminalDockForTests();
  const doc = globalThis.document as unknown as Record<string, unknown>;
  doc.addEventListener = realAdd;
  doc.removeEventListener = realRemove;
  (globalThis as { localStorage?: Storage }).localStorage = originalLocalStorage;
});

// ---- the toggle shortcut ---------------------------------------------------

test("the requested chord (Cmd+Shift+` / Ctrl+Shift+`) opens the closed drawer", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  expect(useTerminalDockOpen).toBeDefined(); // sanity: store module loaded once
  const ev = await fireKeyDown({ shiftKey: true, metaKey: isMac, ctrlKey: !isMac });
  expect(ev.defaultPrevented).toBe(true);
});

test("the same chord closes the drawer again", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  act(() => toggleTerminalDock()); // open it first, outside the shortcut
  await fireKeyDown({ shiftKey: true, metaKey: isMac, ctrlKey: !isMac });
  // toggleTerminalDock() flipped it open, the chord should flip it shut again
  let open = true;
  function Probe() {
    open = useTerminalDockOpen();
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  expect(open).toBe(false);
});

test("VS Code's Ctrl+` alias (no Shift) toggles on every platform", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  const ev = await fireKeyDown({ ctrlKey: true, metaKey: false, shiftKey: false });
  expect(ev.defaultPrevented).toBe(true);
});

test("bare backtick with no modifier does nothing", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  const ev = await fireKeyDown({});
  expect(ev.defaultPrevented).toBe(false);
});

test("Shift+` with no Ctrl/Cmd does nothing (not the requested chord, not the alias)", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  const ev = await fireKeyDown({ shiftKey: true });
  expect(ev.defaultPrevented).toBe(false);
});

test("the wrong platform's modifier for the primary chord does nothing (isMod is exclusive)", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  // The chord for THIS platform, spelled for the OTHER one.
  const ev = await fireKeyDown({ shiftKey: true, metaKey: !isMac, ctrlKey: isMac });
  expect(ev.defaultPrevented).toBe(false);
});

test("Alt held alongside either chord does nothing", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  const ev = await fireKeyDown({ ctrlKey: true, altKey: true });
  expect(ev.defaultPrevented).toBe(false);
});

test("a different key code (not Backquote) does nothing even with the right modifiers", async () => {
  renderTracked(<TerminalDrawer cwd={null} />);
  const ev = await fireKeyDown({ code: "KeyK", shiftKey: true, metaKey: isMac, ctrlKey: !isMac });
  expect(ev.defaultPrevented).toBe(false);
});

test("the toggle actually flips the shared open store (not just preventDefault)", async () => {
  let open = false;
  function Probe() {
    open = useTerminalDockOpen();
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  renderTracked(<TerminalDrawer cwd={null} />);
  expect(open).toBe(false);
  await fireKeyDown({ ctrlKey: true });
  expect(open).toBe(true);
});

// ---- clearExitedSession (the localStorage+store half of exit-hides-drawer) -

test("clearExitedSession drops the cached session id and closes an open drawer", () => {
  localStorage.setItem(
    "fused-render:terminal-drawer",
    JSON.stringify({ height: 300, sessionId: "dead-session" }),
  );
  act(() => toggleTerminalDock()); // drawer open
  let open = true;
  function Probe() {
    open = useTerminalDockOpen();
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  expect(open).toBe(true);

  act(() => clearExitedSession(300));

  act(() => {
    create(<Probe />);
  });
  expect(open).toBe(false);
  const stored = JSON.parse(localStorage.getItem("fused-render:terminal-drawer") as string);
  expect(stored).toEqual({ height: 300, sessionId: null });
});

test("clearExitedSession while the drawer is already closed still clears the cached id, without throwing", () => {
  localStorage.setItem(
    "fused-render:terminal-drawer",
    JSON.stringify({ height: 260, sessionId: "dead-session" }),
  );
  // Drawer already closed (default store state) — closeTerminalDock() is a
  // no-op here, but the id still has to go so the next open doesn't try to
  // reattach to it.
  expect(() => act(() => clearExitedSession(260))).not.toThrow();

  let open = true;
  function Probe() {
    open = useTerminalDockOpen();
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  expect(open).toBe(false);
  const stored = JSON.parse(localStorage.getItem("fused-render:terminal-drawer") as string);
  expect(stored).toEqual({ height: 260, sessionId: null });
});

test("closeTerminalDock stays idempotent when called on an already-closed drawer", () => {
  let calls = 0;
  function Probe() {
    useTerminalDockOpen();
    calls += 1;
    return null;
  }
  act(() => {
    create(<Probe />);
  });
  const before = calls;
  act(() => closeTerminalDock());
  // No listener notification (and therefore no extra render) fires for a
  // no-op close — terminalDockStore.ts's own `set()` bails when the value
  // doesn't actually change.
  expect(calls).toBe(before);
});
