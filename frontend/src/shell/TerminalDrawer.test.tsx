// TerminalDrawer.tsx owns two things this suite covers directly, both
// without ever mounting `TerminalView` (deliberately untested per its own
// header — a headless renderer cannot run its real resize/layout pass, and a
// test-only prop to fake that pass is deliberately not added; see
// DECISIONS.md):
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
} from "@platform/lib/terminalDockStore";

installDomShim();
// terminalSession.ts (imported by TerminalDrawer.tsx) pulls in api.ts ->
// presence.ts -> router.ts, which reads `location` at MODULE SCOPE — the
// shim has to be in place before that import evaluates, so this is a
// dynamic import exactly like terminalSession.test.ts's own.
const TerminalDrawerModule = await import("@shell/TerminalDrawer");
const TerminalDrawer = TerminalDrawerModule.default;
const { clearExitedSession, createSessionOrAbandon, sendPendingRequestIfAny, TerminalBusyError } =
  TerminalDrawerModule;
const { parseState, reconcileTabs, removeTab, programLabel, stateFor } = await import("@shell/terminalTabs");

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

test("clearExitedSession drops every cached session id and closes an open drawer", () => {
  localStorage.setItem(
    "fused-render:terminal-drawer",
    JSON.stringify({ height: 300, sessionIds: ["dead-session"], activeId: "dead-session" }),
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
  expect(stored).toEqual({ height: 300, sessionIds: [], activeId: null, meta: {} });
});

test("clearExitedSession while the drawer is already closed still clears the cached id, without throwing", () => {
  localStorage.setItem(
    "fused-render:terminal-drawer",
    JSON.stringify({ height: 260, sessionIds: ["dead-session"], activeId: "dead-session" }),
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
  expect(stored).toEqual({ height: 260, sessionIds: [], activeId: null, meta: {} });
});

// ---- createSessionOrAbandon (cancelled-create leaks no live shell) --------

test("create resolving after cancel kills that id and returns null instead of setting state", async () => {
  const killed: string[] = [];
  const result = await createSessionOrAbandon(undefined, () => true, {
    create: async () => "new-id",
    kill: async (id: string) => {
      killed.push(id);
      return { ok: true };
    },
  });
  expect(result).toBeNull();
  expect(killed).toEqual(["new-id"]);
});

test("create resolving before cancel returns the id and never kills it", async () => {
  const killed: string[] = [];
  const result = await createSessionOrAbandon(undefined, () => false, {
    create: async () => "new-id",
    kill: async (id: string) => {
      killed.push(id);
      return { ok: true };
    },
  });
  expect(result).toBe("new-id");
  expect(killed).toEqual([]);
});

test("a kill that itself rejects is swallowed, not thrown back at the caller", async () => {
  const result = await createSessionOrAbandon(undefined, () => true, {
    create: async () => "new-id",
    kill: async () => {
      throw new Error("server unreachable");
    },
  });
  expect(result).toBeNull();
});

// ---- sendPendingRequestIfAny (consume-once "open here / run this") -------

test("no pending request: does not call send", async () => {
  const sent: unknown[] = [];
  await sendPendingRequestIfAny(
    "sid-1",
    {},
    { take: () => null, send: async (...args) => { sent.push(args); return { ok: true }; } },
  );
  expect(sent).toEqual([]);
});

test("cwd+command: sends the full cd-and-run string", async () => {
  const sent: unknown[] = [];
  await sendPendingRequestIfAny(
    "sid-1",
    {},
    {
      take: () => ({ cwd: "/tmp/foo", command: "ls" }),
      send: async (id, data) => { sent.push([id, data]); return { ok: true }; },
    },
  );
  expect(sent).toEqual([["sid-1", "cd '/tmp/foo' && ls\r"]]);
});

test("createdCwd matching the request's cwd: sends only the command, since the session was created in that cwd already", async () => {
  const sent: unknown[] = [];
  await sendPendingRequestIfAny(
    "sid-1",
    { createdCwd: "/tmp/foo" },
    {
      take: () => ({ cwd: "/tmp/foo", command: "ls" }),
      send: async (id, data) => { sent.push([id, data]); return { ok: true }; },
    },
  );
  expect(sent).toEqual([["sid-1", "ls\r"]]);
});

test("createdCwd matching the request's cwd, no command left: nothing to send", async () => {
  const sent: unknown[] = [];
  await sendPendingRequestIfAny(
    "sid-1",
    { createdCwd: "/tmp/foo" },
    { take: () => ({ cwd: "/tmp/foo" }), send: async (...args) => { sent.push(args); return { ok: true }; } },
  );
  expect(sent).toEqual([]);
});

test("createdCwd NOT matching the request's cwd (a newer request replaced the peeked one): still sends the cd", async () => {
  const sent: unknown[] = [];
  await sendPendingRequestIfAny(
    "sid-1",
    { createdCwd: "/tmp/foo" },
    {
      take: () => ({ cwd: "/tmp/bar", command: "ls" }),
      send: async (id, data) => { sent.push([id, data]); return { ok: true }; },
    },
  );
  expect(sent).toEqual([["sid-1", "cd '/tmp/bar' && ls\r"]]);
});

test("take() is called exactly once per invocation (consume-once is the caller's job via the store, not re-checked here)", async () => {
  let calls = 0;
  await sendPendingRequestIfAny(
    "sid-1",
    {},
    {
      take: () => { calls += 1; return { command: "ls" }; },
      send: async () => ({ ok: true }),
    },
  );
  expect(calls).toBe(1);
});

// ---- sendPendingRequestIfAny: the pty-busy (409) fallback -----------------

test("409 on send: copies the plain command to the clipboard and throws TerminalBusyError instead of the raw error", async () => {
  const copied: string[] = [];
  const attempt = sendPendingRequestIfAny(
    "sid-1",
    {},
    {
      take: () => ({ cwd: "/tmp/foo", command: "ls" }),
      send: async () => {
        throw Object.assign(new Error("terminal is busy"), { status: 409 });
      },
      copy: async (text: string) => {
        copied.push(text);
        return true;
      },
    },
  );
  await expect(attempt).rejects.toThrow(TerminalBusyError);
  // Bracketed-paste/`\r` framing stripped: the clipboard gets plain text a
  // person would actually want to paste, not the raw control bytes.
  expect(copied).toEqual(["cd '/tmp/foo' && ls"]);
});

test("409 with no cwd (command only): still copies the plain command, `\\r` stripped", async () => {
  const copied: string[] = [];
  const attempt = sendPendingRequestIfAny(
    "sid-1",
    {},
    {
      take: () => ({ command: "claude" }),
      send: async () => {
        throw Object.assign(new Error("busy"), { status: 409 });
      },
      copy: async (text: string) => {
        copied.push(text);
        return true;
      },
    },
  );
  await expect(attempt).rejects.toThrow(TerminalBusyError);
  expect(copied).toEqual(["claude"]);
});

test("a non-409 send failure propagates as-is, with no clipboard fallback", async () => {
  const copied: string[] = [];
  const boom = Object.assign(new Error("connection dropped"), { status: 500 });
  const attempt = sendPendingRequestIfAny(
    "sid-1",
    {},
    {
      take: () => ({ command: "ls" }),
      send: async () => {
        throw boom;
      },
      copy: async (text: string) => {
        copied.push(text);
        return true;
      },
    },
  );
  await expect(attempt).rejects.toBe(boom);
  expect(copied).toEqual([]);
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

// ---- the busy (409) fix: a long-running opener gets its OWN terminal -------

function busy(): Error {
  return Object.assign(new Error("terminal is busy"), { status: 409 });
}

test("REGRESSION: a second `claude --resume` for a different task opens a NEW terminal instead of copying to the clipboard", async () => {
  const sent: Array<[string, string]> = [];
  const copied: string[] = [];
  const adopted: Array<[string, unknown]> = [];
  const created: Array<string | undefined> = [];
  // sidA's terminal is owned by Claude's TUI after the first continue.
  const send = async (id: string, data: string) => {
    sent.push([id, data]);
    if (id === "term-1" && data.includes("sidB")) throw busy();
    return { ok: true };
  };
  const create = async (cwd?: string) => {
    created.push(cwd);
    return "term-2";
  };
  const adopt = (id: string, tab: unknown) => adopted.push([id, tab]);
  const copy = async (t: string) => {
    copied.push(t);
    return true;
  };

  await sendPendingRequestIfAny("term-1", {}, {
    take: () => ({ cwd: "/work/a", command: "claude --resume sidA" }),
    send, copy, create, adopt,
  });
  await sendPendingRequestIfAny("term-1", {}, {
    take: () => ({ cwd: "/work/b", command: "claude --resume sidB" }),
    send, copy, create, adopt,
  });

  expect(created).toEqual(["/work/b"]);
  expect(adopted).toEqual([["term-2", { label: "claude", cwd: "/work/b" }]]);
  // sidA once, to the first terminal; sidB tried on the busy one, then sent
  // to the new one — created in /work/b, so no `cd`.
  expect(sent).toEqual([
    ["term-1", "cd '/work/a' && claude --resume sidA\r"],
    ["term-1", "cd '/work/b' && claude --resume sidB\r"],
    ["term-2", "claude --resume sidB\r"],
  ]);
  expect(copied).toEqual([]);
});

test("busy terminal and the create hits the server cap (409): copies, and the error says the limit was reached", async () => {
  const copied: string[] = [];
  const attempt = sendPendingRequestIfAny("t1", {}, {
    take: () => ({ cwd: "/w", command: "claude" }),
    send: async () => { throw busy(); },
    copy: async (t) => { copied.push(t); return true; },
    create: async () => { throw busy(); },
    adopt: () => { throw new Error("must not adopt"); },
  });
  const err = await attempt.then(() => null, (e) => e);
  expect(err).toBeInstanceOf(TerminalBusyError);
  expect((err as InstanceType<typeof TerminalBusyError>).reason).toBe("limit");
  expect(copied).toEqual(["cd '/w' && claude"]);
});

test("busy terminal and the create fails another way: copies with the plain busy reason", async () => {
  const copied: string[] = [];
  const err = await sendPendingRequestIfAny("t1", {}, {
    take: () => ({ command: "claude" }),
    send: async () => { throw busy(); },
    copy: async (t) => { copied.push(t); return true; },
    create: async () => { throw Object.assign(new Error("down"), { status: 500 }); },
    adopt: () => {},
  }).then(() => null, (e) => e);
  expect((err as InstanceType<typeof TerminalBusyError>).reason).toBe("busy");
  expect(copied).toEqual(["claude"]);
});

test("a cd-only request on a busy terminal creates a terminal in that cwd and sends nothing more", async () => {
  const sent: string[] = [];
  const adopted: unknown[] = [];
  await sendPendingRequestIfAny("t1", {}, {
    take: () => ({ cwd: "/only/cd" }),
    send: async (id, d) => {
      sent.push(id + ":" + d);
      if (id === "t1") throw busy();
      return { ok: true };
    },
    create: async () => "t2",
    adopt: (id, tab) => adopted.push([id, tab]),
  });
  expect(sent).toEqual(["t1:cd '/only/cd'\r"]);
  expect(adopted).toEqual([["t2", { label: "Terminal", cwd: "/only/cd" }]]);
});

test("the fresh terminal itself reporting busy copies instead of looping into a third terminal", async () => {
  let creates = 0;
  const copied: string[] = [];
  const err = await sendPendingRequestIfAny("t1", {}, {
    take: () => ({ cwd: "/w", command: "claude" }),
    send: async () => { throw busy(); },
    copy: async (t) => { copied.push(t); return true; },
    create: async () => { creates += 1; return "t2"; },
    adopt: () => {},
  }).then(() => null, (e) => e);
  expect(creates).toBe(1);
  expect(err).toBeInstanceOf(TerminalBusyError);
  expect(copied).toEqual(["cd '/w' && claude"]);
});

// ---- terminalTabs: persistence, restore, neighbour rules, labels ----------

test("programLabel: first word, path and env assignments stripped", () => {
  expect(programLabel("claude --resume abc")).toBe("claude");
  expect(programLabel("/usr/local/bin/claude")).toBe("claude");
  expect(programLabel("FOO=1 BAR=2 npm run dev")).toBe("npm");
  expect(programLabel("")).toBeNull();
  expect(programLabel(undefined)).toBeNull();
});

test("parseState migrates the old single-sessionId shape", () => {
  expect(parseState(JSON.stringify({ height: 300, sessionId: "old" }))).toEqual({
    height: 300, sessionIds: ["old"], activeId: "old", meta: {},
  });
});

test("parseState: new shape round-trips; junk and unknown active fall back sanely", () => {
  const st = stateFor(
    400,
    [{ id: "a", label: "zsh", cwd: "/x" }, { id: "b", label: "claude" }],
    "b",
  );
  expect(parseState(JSON.stringify(st))).toEqual(st);
  expect(parseState("not json").sessionIds).toEqual([]);
  expect(parseState(null).height).toBe(260);
  expect(parseState(JSON.stringify({ sessionIds: ["a", "a", 5], activeId: "zzz" }))).toMatchObject({
    sessionIds: ["a"], activeId: null,
  });
});

test("reconcileTabs keeps alive ids in cached order, drops dead and vanished, restores the active one", () => {
  const cached = parseState(
    JSON.stringify({ sessionIds: ["a", "b", "c", "d"], activeId: "c", meta: { b: { label: "claude" } } }),
  );
  const r = reconcileTabs(cached, [
    { id: "d", alive: true, shell: "zsh" },
    { id: "b", alive: true, shell: "zsh" },
    { id: "c", alive: false },
    { id: "a", alive: true, shell: "zsh", cwd: "/home" },
  ]);
  expect(r.tabs).toEqual([
    { id: "a", label: "zsh", cwd: "/home" },
    { id: "b", label: "claude" },
    { id: "d", label: "zsh" },
  ]);
  // the cached active tab (c) died -> first survivor
  expect(r.activeId).toBe("a");
  expect(reconcileTabs({ ...cached, activeId: "d" }, [{ id: "d", alive: true }]).activeId).toBe("d");
});

test("reconcileTabs: nothing survives -> empty (caller creates a fresh terminal)", () => {
  expect(reconcileTabs(parseState(JSON.stringify({ sessionId: "x" })), [])).toEqual({ tabs: [], activeId: null });
});

const T = (id: string) => ({ id, label: id });

test("removeTab: closing the active middle tab activates its right neighbour", () => {
  const r = removeTab([T("a"), T("b"), T("c")], "b", "b");
  expect(r).toEqual({ tabs: [T("a"), T("c")], activeId: "c", empty: false });
});

test("removeTab: closing the active LAST tab activates the left neighbour", () => {
  expect(removeTab([T("a"), T("b")], "b", "b").activeId).toBe("a");
});

test("removeTab: closing an inactive tab keeps the active one", () => {
  expect(removeTab([T("a"), T("b")], "a", "b")).toEqual({ tabs: [T("a")], activeId: "a", empty: false });
});

test("removeTab: the only tab leaves nothing -> empty (drawer closes)", () => {
  expect(removeTab([T("a")], "a", "a")).toEqual({ tabs: [], activeId: null, empty: true });
});
