// THE SCHEDULE HOP, END TO END — a URL goes in, a filled-in New task card comes
// out (Akshil, 2026-09-16).
//
// Everything else about this hop is pinned by reading source strings, which is
// cheap and says nothing about whether the card ever appears: the arm reads a
// param, fetches, seeds and opens across three ticks and a promise, and the
// live failure it was written for — the URL flashing past and collapsing to a
// bare `/tasks` with no dialog at all — would have passed every one of those
// assertions. So this one mounts the page on the URL and looks at the card.
//
// A stubbed `globalThis.fetch`, not `mock.module`, for the reason
// `Indexing.render.test.tsx`'s header gives: the modules under test are thin
// wrappers over `fetch`, and replacing one process-wide leaks into every other
// suite in the run.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, expect, test } from "bun:test";
import { act, create, type ReactTestRenderer } from "react-test-renderer";
import { createElement } from "react";

const realFetch = globalThis.fetch;
const doc = globalThis.document as unknown as { body?: unknown };

/**
 * A CONTAINER FOR THE CARD TO PORTAL INTO, installed for these three tests and
 * taken away again.
 *
 * The modal chassis portals into `document.body` (`platform/ui/modal/Modal`),
 * and react-dom's `createPortal` throws "Target container is not a DOM element"
 * on anything whose `nodeType` is not an element's — from inside the render,
 * which unmounts the tree to the root and takes the assertions with it. The
 * renderer draws a portal's children in place and never looks at the container,
 * so an element by `nodeType` with a `children` list is the whole of it.
 *
 * NOT IN `testDomShim`, deliberately: `bun test` shares one `globalThis` across
 * every suite in the run, and a standing `document.body` changes what OTHER
 * libraries decide to do. Base UI's `FloatingPortal` asks whether there is a
 * body and, finding one, reaches for `document.createElement` to make itself a
 * host — which the shim's document does not have, so the popover suites that
 * pass today would start throwing on a container of `undefined`. The
 * shim stays the document those suites already agree on; this is one suite's
 * fixture, removed in `afterEach`.
 *
 * `createNodeMock` rides on it because `react-test-renderer` resolves a ref
 * against the CONTAINER its node landed in — the one passed to `create()` never
 * reaches anything inside a portal.
 */
function installBody() {
  doc.body = { nodeType: 1, children: [] as unknown[], createNodeMock: node };
}

/** "There was no such global", which is not the same answer as `undefined` —
 *  one is a `ReferenceError` on read and the other is not. */
const MISSING = Symbol("missing");

/** What was on `globalThis` under each name `installMeasuring` took over, so
 *  the teardown puts the run back exactly as it found it. */
const measuringWas = new Map<string, unknown>();

/**
 * THE MEASURING APPARATUS THE PAGE REACHES FOR ON MOUNT, for the length of
 * these three tests: two observer constructors and `getComputedStyle`.
 *
 * This is what the suite was failing on, and it is an environment gap rather
 * than a race. `Scheduled`'s list measurer and `TaskPeek`'s host each do a bare
 * `new ResizeObserver(...)` in a LAYOUT effect, and `row-fit` reaches for a
 * `MutationObserver` and `getComputedStyle` in another — none of them guarded,
 * because in a browser there is nothing to guard. A throw from a layout effect
 * is not a failed assertion: React unmounts the tree to the root. So the card
 * never appears — the 2 s `waitForDialog` poll just runs out — and every later
 * read of `box.root` dies with "Can't access .root on unmounted test renderer",
 * which names the symptom and not one word of the cause. The previous round
 * read that as timing and answered it with a poll; no poll can wait its way to
 * a constructor that is not there.
 *
 * TAKEN OVER UNCONDITIONALLY, not installed only when missing — that is the
 * half-measure that made this look CI-only. `bun test` shares one `globalThis`
 * across the whole run and suites leave their own measuring stubs on it, cut to
 * their own fixtures: `row-fit.test.ts`'s `fakeRow` alone leaves behind a
 * permanent `getComputedStyle` that reads a `__style` property off the node it
 * is handed, which answers `undefined` for every node in THIS suite. So which
 * error the page died of came down to where the runner happened to reach this
 * file: a bare global on CI (`ReferenceError: ResizeObserver is not defined`),
 * somebody else's leftover locally (`undefined is not an object (evaluating
 * 'cs.columnGap')`). One cause, two spellings. The suite states all three
 * itself and hands back whatever was there.
 *
 * Inert on purpose: nothing in these tests resizes or mutates, so a delivery
 * road would have nothing to deliver, and every style reads as the empty string
 * — which is what an unstyled element answers anyway, and what every caller
 * here already rounds to zero.
 *
 * They do NOT belong in `testDomShim`: a standing global would quietly flip the
 * suites that deliberately exercise the no-observer path (`Transcript`'s
 * `settled` starts true where there is nothing to measure) or count calls
 * through a stub of their own.
 */
function installMeasuring() {
  const g = globalThis as Record<string, unknown>;
  const inert = class {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [] as unknown[];
    }
  };
  const stubs: Record<string, unknown> = {
    ResizeObserver: inert,
    MutationObserver: inert,
    getComputedStyle: () => new Proxy({}, { get: () => "" }),
  };
  for (const [name, stub] of Object.entries(stubs)) {
    if (!measuringWas.has(name)) measuringWas.set(name, name in g ? g[name] : MISSING);
    g[name] = stub;
  }
}

function removeMeasuring() {
  const g = globalThis as Record<string, unknown>;
  for (const [name, before] of measuringWas) {
    if (before === MISSING) delete g[name];
    else g[name] = before;
  }
  measuringWas.clear();
}

/** Everything the card's mount effects reach for on a real element. */
function node() {
  return {
    focus() {},
    blur() {},
    select() {},
    setSelectionRange() {},
    scrollIntoView() {},
    addEventListener() {},
    removeEventListener() {},
    contains: () => false,
    closest: () => null,
    querySelector: () => null,
    querySelectorAll: () => [] as unknown[],
    // `row-fit`'s measurer walks `scope.children` — an absent list is an
    // `Array.from(undefined)` throw out of the same layout effect.
    children: [] as unknown[],
    style: {} as Record<string, string>,
    value: "",
    getBoundingClientRect: () => ({
      top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, x: 0, y: 0,
    }),
  };
}

// THE RENDERER THE CURRENT TEST IS HOLDING, so a failure that skips a test's
// own `finally` still gets torn down before the next test's `installBody()`
// replaces `doc.body` out from under it.
//
// Why this matters (the CI-only flake this file used to have): a leaked,
// still-mounted box keeps its real `setTimeout`s alive — TaskPeek's
// `PEEK_SETTLE_MS`/`PEEK_PARK_MS` among them. `afterEach` used to
// unconditionally `delete doc.body` with nothing keeping that box's own
// effects from firing later, against a `document.body` that had since been
// replaced or deleted — and a portal effect finding no element there throws
// "Target container is not a DOM element" deep inside the tree, which (no
// error boundary here) unmounts that box to the root and prints "The above
// error occurred in the <TaskPeek>/<Scheduled> component" — noise that lands
// on whichever test happens to be running when the stale timer fires. On a
// fast machine every test's own `finally` reaches its `closeBox` well before
// any of that, so the box is long gone and its timers cleared (effect
// cleanup) before the next test starts; a loaded CI runner is exactly where
// that race stops being theoretical.
let lastBox: ReactTestRenderer | null = null;

afterEach(async () => {
  if (lastBox) {
    const box = lastBox;
    lastBox = null;
    await act(async () => box.unmount());
  }
  globalThis.fetch = realFetch;
  removeMeasuring();
  delete doc.body;
});

function json(body: unknown): Promise<Response> {
  return Promise.resolve(
    { ok: true, status: 200, json: () => Promise.resolve(body) } as unknown as Response,
  );
}

/** One chat record under one key, and a server that answers everything else
 *  this page asks with the emptiest true answer it has. */
function serve(chat: Record<string, unknown>) {
  globalThis.fetch = ((url: string) => {
    const u = String(url);
    if (u.startsWith("/api/drafts")) return json({ chat, task: {} });
    if (u.startsWith("/api/schedule/queue")) return json({ queued: [], running: [] });
    if (u.startsWith("/api/schedule")) return json({ entries: [], permission_modes: ["default"] });
    // The long poll is a request that never answers — the page must not be
    // waiting on it to draw anything.
    if (u.includes("/api/tasks/changes")) return new Promise<Response>(() => {});
    if (u.startsWith("/api/tasks")) return json({ tasks: [] });
    if (u.startsWith("/api/config")) return json({ home: "/Users/me" });
    return json({ folders: [], entries: [], models: [], tasks: [] });
  }) as unknown as typeof fetch;
}

function record(over: Record<string, unknown> = {}) {
  return {
    text: "Water the plants\n\nthe ones on the sill",
    attachments: [],
    updated_at: 1,
    version: 3,
    bound_draft: "",
    form: {},
    ...over,
  };
}

/** Is the card's dialog up? Wrapped because `.root` itself throws on a tree
 *  that has (even transiently) zero children — which a poll must read as
 *  "not yet", not crash on. */
function hasDialog(box: ReactTestRenderer): boolean {
  try {
    return box.root.findAll((n) => n.props?.role === "dialog", { deep: true }).length > 0;
  } catch {
    return false;
  }
}

/**
 * POLL FOR THE CARD, rather than guess a fixed sleep long enough to cover it.
 *
 * The `?new=1&draft=` arm's own chain — `fetch("/api/drafts")` → `openForm`
 * — is nothing but chained microtasks against a stub that resolves
 * synchronously, so an `act(async () => { await Promise.resolve() })` loop
 * flushes it deterministically; the zero-delay `setTimeout` a couple of
 * effects ride on (`installDomShim`'s `requestAnimationFrame` shim, Base UI's
 * own frame bookkeeping) needs one real tick to fire. A flat sleep picked a
 * number of milliseconds that happened to cover both on this machine; a
 * loaded CI box does not owe that number anything, and the previous flake —
 * assertions running before the card had opened — traces straight back to
 * it. Bounded generously (2s) so a genuine regression still fails fast
 * rather than hanging.
 */
async function waitForDialog(box: ReactTestRenderer, timeoutMs = 2000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    await act(async () => {
      await new Promise((done) => setTimeout(done, 0));
    });
    if (hasDialog(box)) return;
    if (Date.now() >= deadline) return; // let the caller's own assertion report it
    await act(async () => {
      await new Promise((done) => setTimeout(done, 10));
    });
  }
}

/** Mount the Tasks page ON a URL, the way a hop arrives at it. */
async function hopTo(search: string) {
  installBody();
  installMeasuring();
  const loc = globalThis.location as unknown as { search: string; pathname: string };
  loc.pathname = "/tasks";
  loc.search = search;
  const { default: Scheduled } = await import("./Scheduled");
  let box!: ReactTestRenderer;
  await act(async () => {
    // The card's own refs resolve against the stand-in body's maker; this one
    // covers the page behind it.
    box = create(createElement(Scheduled), { createNodeMock: node });
  });
  // Registered the moment it exists — not after this function returns — so a
  // throw from `waitForDialog` below still leaves something for `afterEach`
  // to close.
  lastBox = box;
  await waitForDialog(box);
  return box;
}

/** The one door that unmounts a box AND retires it from `afterEach`'s
 *  safety net — every test's `finally` goes through this, not `box.unmount()`
 *  directly, so the two never disagree about whether it is still live. */
async function closeBox(box: ReactTestRenderer): Promise<void> {
  await act(async () => box.unmount());
  if (lastBox === box) lastBox = null;
}

/** What the card is showing, by the labels a reader sees. */
function fields(box: ReactTestRenderer): Record<string, string> {
  const out: Record<string, string> = {};
  for (const node of box.root.findAll(
    (n) => typeof n.type === "string" && (n.type === "input" || n.type === "textarea"),
    { deep: true },
  )) {
    const label = (node.props["aria-label"] ?? node.props.placeholder ?? "") as string;
    if (label && out[label] === undefined) out[label] = String(node.props.value ?? "");
  }
  return out;
}

test("a `new:` hop opens the card on that record — title, words and folder", async () => {
  serve({ "new:/tmp/lab": record() });
  const box = await hopTo(
    "?new=1&draft=" + encodeURIComponent("new:/tmp/lab")
    + "&target=" + encodeURIComponent("/tmp/lab")
    + "&from=" + encodeURIComponent("/explorer/view/tmp/lab?_side=claude"),
  );
  try {
    const seen = fields(box);
    // The card is UP — the live failure was no dialog at all, so this is the
    // assertion the rest hang off.
    expect(hasDialog(box)).toBe(true);
    expect(seen["What should Claude do?"]).toBe("Water the plants");
    expect(seen["Additional instructions"]).toBe("the ones on the sill");
    expect(seen["Add folder or file"]).toBe("/tmp/lab");
  } finally {
    // ALWAYS — an assertion above throwing must not skip this. A box left
    // mounted keeps ticking (TaskPeek's own timers among them) past this
    // test's end, into whatever `doc.body` the NEXT test installs.
    await closeBox(box);
  }
});

test("a SESSION hop lands in the session's folder, not the reader's home", async () => {
  // The bug: a session key spells no path, `chatHopSeed` fell through to `""`,
  // and the card opened on `~` — then wrote that home path onto the
  // conversation's own record with its first autosave.
  const session = "11111111-2222-3333-4444-555555555555";
  serve({ [session]: record({ text: "Ship the notes\n\nthen tell Ada" }) });
  const box = await hopTo(
    "?new=1&draft=" + session
    + "&target=" + encodeURIComponent("/tmp/lab")
    + "&from=" + encodeURIComponent("/explorer/view/tmp/lab?_side=claude"),
  );
  try {
    const seen = fields(box);
    expect(seen["What should Claude do?"]).toBe("Ship the notes");
    expect(seen["Additional instructions"]).toBe("then tell Ada");
    expect(seen["Add folder or file"]).toBe("/tmp/lab");
  } finally {
    await closeBox(box);
  }
});

test("a record that names its own target still wins over the hop's", async () => {
  const session = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
  serve({ [session]: record({ form: { target: "/tmp/chosen" } }) });
  const box = await hopTo(
    "?new=1&draft=" + session + "&target=" + encodeURIComponent("/tmp/lab"),
  );
  try {
    expect(fields(box)["Add folder or file"]).toBe("/tmp/chosen");
  } finally {
    await closeBox(box);
  }
});
