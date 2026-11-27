import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { pauseWhileHidden, TAB_VISIBILITY_EVENT } from "./pause-while-hidden";

// Fake windows and a fake document, swapped onto globalThis per test and put
// back after: the gate reads both at call time, never at import.
type Listener = () => void;
function fakeWin(parent?: FakeWin): FakeWin {
  const listeners = new Map<string, Set<Listener>>();
  const w: FakeWin = {
    parent: undefined as unknown as FakeWin,
    addEventListener: (t, fn) => {
      if (!listeners.has(t)) listeners.set(t, new Set());
      listeners.get(t)!.add(fn);
    },
    removeEventListener: (t, fn) => listeners.get(t)?.delete(fn),
    fire: (t) => listeners.get(t)?.forEach((fn) => fn()),
    count: (t) => listeners.get(t)?.size ?? 0,
  };
  w.parent = parent ?? w;
  return w;
}
interface FakeWin {
  parent: FakeWin;
  _fusedWorkspace?: object;
  _fusedTabHidden?: boolean;
  addEventListener(t: string, fn: Listener): void;
  removeEventListener(t: string, fn: Listener): void;
  fire(t: string): void;
  count(t: string): number;
}

const g = globalThis as { window?: unknown; document?: unknown };
let savedWindow: unknown;
let savedDocument: unknown;
let doc: FakeWin & { visibilityState: string };

beforeEach(() => {
  savedWindow = g.window;
  savedDocument = g.document;
  doc = Object.assign(fakeWin(), { visibilityState: "visible" });
  g.document = doc;
});
afterEach(() => {
  g.window = savedWindow;
  g.document = savedDocument;
});

function frameOfHost(): { host: FakeWin; tab: FakeWin } {
  const host = fakeWin();
  host._fusedWorkspace = {};
  const tab = fakeWin(host);
  return { host, tab };
}

describe("pauseWhileHidden", () => {
  test("a visible top-level window runs straight through", () => {
    g.window = fakeWin();
    let runs = 0;
    pauseWhileHidden(() => runs++)();
    expect(runs).toBe(1);
  });

  test("a hidden document parks, and catches up once on return", () => {
    g.window = fakeWin();
    doc.visibilityState = "hidden";
    let runs = 0;
    const gated = pauseWhileHidden(() => runs++);
    gated();
    gated();
    expect(runs).toBe(0);
    doc.visibilityState = "visible";
    doc.fire("visibilitychange");
    expect(runs).toBe(1);
  });

  test("a workspace frame the host marks hidden parks until the host shows it", () => {
    const { tab } = frameOfHost();
    g.window = tab;
    tab._fusedTabHidden = true;
    let runs = 0;
    const gated = pauseWhileHidden(() => runs++);
    gated();
    gated();
    expect(runs).toBe(0);
    expect(tab.count(TAB_VISIBILITY_EVENT)).toBe(1);
    tab._fusedTabHidden = false;
    tab.fire(TAB_VISIBILITY_EVENT);
    expect(runs).toBe(1);
    // Detached once caught up: a later event runs nothing.
    expect(tab.count(TAB_VISIBILITY_EVENT)).toBe(0);
    tab.fire(TAB_VISIBILITY_EVENT);
    expect(runs).toBe(1);
  });

  test("a frame inside a hidden workspace frame is hidden with it", () => {
    const { tab } = frameOfHost();
    const card = fakeWin(tab);
    g.window = card;
    tab._fusedTabHidden = true;
    let runs = 0;
    pauseWhileHidden(() => runs++)();
    expect(runs).toBe(0);
    tab._fusedTabHidden = false;
    tab.fire(TAB_VISIBILITY_EVENT);
    expect(runs).toBe(1);
  });

  test("the shown frame of a hidden window stays parked until both say visible", () => {
    const { tab } = frameOfHost();
    g.window = tab;
    tab._fusedTabHidden = true;
    doc.visibilityState = "hidden";
    let runs = 0;
    pauseWhileHidden(() => runs++)();
    tab._fusedTabHidden = false;
    tab.fire(TAB_VISIBILITY_EVENT);
    expect(runs).toBe(0);
    doc.visibilityState = "visible";
    doc.fire("visibilitychange");
    expect(runs).toBe(1);
  });

  test("cancel drops a parked catch-up", () => {
    const { tab } = frameOfHost();
    g.window = tab;
    tab._fusedTabHidden = true;
    let runs = 0;
    const gated = pauseWhileHidden(() => runs++);
    gated();
    gated.cancel();
    tab._fusedTabHidden = false;
    tab.fire(TAB_VISIBILITY_EVENT);
    expect(runs).toBe(0);
  });
});
