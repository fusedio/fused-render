// A handful of platform/lib suites import modules that read `window`,
// `location`, or `history` at MODULE SCOPE (router.ts's IS_EMBED, appShot.ts's
// pointerdown listener) because that is genuinely when those modules need to
// know their environment — a page load, not a function call. Bun's test
// runtime has no DOM, so those reads need something on `globalThis` before the
// import happens.
//
// Every suite that does this shares one `globalThis`, so whichever suite's
// import runs first in the process determines what every OTHER suite's module
// code sees when it reads `window`/`location`/`history` — bun does not reset
// globals between files in one run. Call `installDomShim()` at the top of a
// test file, before any import of the module under test, and every suite gets
// the exact same stub regardless of which one the runner happens to start
// with. Extend the objects here, in this one place, when a module needs one
// more member off `window`/`location`/`history` — never re-add a competing
// `??=` stub in a test file, or the ordering bug is right back.
export function installDomShim(): void {
  const g = globalThis as {
    location?: unknown;
    history?: unknown;
    window?: unknown;
    Element?: unknown;
    HTMLElement?: unknown;
    HTMLIFrameElement?: unknown;
    requestAnimationFrame?: unknown;
    cancelAnimationFrame?: unknown;
    document?: unknown;
  };
  // React 19's `act` reads `HTMLElement` while it flushes, so a component suite
  // driven by `react-test-renderer` throws before its own assertions run — with
  // a ReferenceError from inside React, which says nothing about the test. A
  // constructor nothing is ever instanceof is enough: the renderer builds plain
  // objects, so the class only has to EXIST.
  g.Element ??= class Element {};
  g.HTMLElement ??= class HTMLElement extends (g.Element as new () => object) {};
  // JobPopupCard's outside-blur detection narrows on
  // `document.activeElement instanceof HTMLIFrameElement` — a real
  // constructor an actual `<iframe>` node satisfies in the browser, so the
  // stand-in only has to be a class a test can `new` up to fake one focused.
  g.HTMLIFrameElement ??=
    class HTMLIFrameElement extends (g.HTMLElement as new () => object) {};
  // Base UI schedules its transition bookkeeping on a frame. There are no frames
  // here, so the next macrotask is the honest stand-in: the callback runs, once,
  // and `act` can flush it.
  g.requestAnimationFrame ??= (cb: (t: number) => void) =>
    globalThis.setTimeout(() => cb(0), 0) as unknown as number;
  g.cancelAnimationFrame ??= (handle: number) => globalThis.clearTimeout(handle);
  // A COMPONENT THAT TICKS is a component that reads both of these. The chat's
  // status line redraws its clock on a 1 s `window.setInterval` and repairs it
  // on `visibilitychange` — with either member missing that effect THREW during
  // commit, which unmounts the whole tree to the root and takes the suite's own
  // assertions with it. `hidden: false` is the honest answer for a renderer that
  // has no window at all: the frame clock's hidden-tab rescue is the exception
  // path, not the one a test should silently take.
  g.document ??= {
    hidden: false,
    activeElement: null,
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  g.location ??= {
    pathname: "/",
    search: "",
    href: "http://localhost/",
    origin: "http://localhost",
    assign() {},
    reload() {},
  };
  g.history ??= {
    state: null,
    pushState() {},
    replaceState() {},
  };
  g.window ??= {
    dispatchEvent() {},
    addEventListener() {},
    removeEventListener() {},
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    // `presence.ts`'s heartbeat (installed as an import side effect, exactly
    // like `notifications.ts`'s own `installIngest()`) reaches for these on
    // EVERY suite that transitively imports it, not just the ones actually
    // testing presence — missing here throws between tests rather than in
    // one, the same class of bug this shim's own header warns about.
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
    // The same class as the global, and it has to be the SAME one: Base UI's
    // `isHTMLElement` tests `value instanceof getWindow(value).HTMLElement`,
    // which throws outright — "right hand side of instanceof is not an object" —
    // when the window it reaches for has no such member.
    // The same object as the global, so a suite can stub `location.assign` on
    // either and the code under test — which reads `window.location` — sees it.
    // LIVE, not a snapshot: suites swap `globalThis.location` for their own
    // object (AppVersionPicker.test.tsx does, per test), and code that reads
    // `window.location` must see the same one — a copied reference here left
    // `window.location.assign` stubs and `globalThis.location` stubs pointing
    // at two different objects (Bugbot, PR #1317).
    get location() {
      return (globalThis as { location?: unknown }).location;
    },
    Element: g.Element,
    HTMLElement: g.HTMLElement,
    requestAnimationFrame: g.requestAnimationFrame,
    cancelAnimationFrame: g.cancelAnimationFrame,
  };
}

/** A `document.body` A MODAL CAN BE PORTALED INTO — installed BY THE SUITE THAT
 *  RENDERS ONE, and taken away again, never by `installDomShim`.
 *
 *  The three odd members are what a portal needs. Every dialog on the shared
 *  chassis (`platform/ui/modal/Modal`) ends in `createPortal(...,
 *  document.body)`, so rendering one hits react-dom's `isValidContainer` (which
 *  wants a `nodeType`), then the test renderer's own `appendChild` (which wants
 *  a `children` ARRAY on the container), and then — for the dialog's `ref` —
 *  `rootContainerInstance.createNodeMock`, read off the PORTAL's container
 *  rather than off the `create()` options, because a portal is its own root.
 *
 *  OPT-IN, AND PAIRED WITH `removePortalContainer`, because a body that merely
 *  EXISTS changes what other suites do: Base UI's `FloatingPortal` (every
 *  shadcn popover) skips portalling entirely while there is no body and tries
 *  to portal into this stub the moment there is one, which fails on the first
 *  DOM call the stub does not answer. `bun test` shares one `globalThis` across
 *  every file in a run, so a body left behind here is a body every later suite
 *  renders against. Install it around the mount, drop it after.
 *
 *  Re-install per mount rather than once per file: several suites REPLACE
 *  `globalThis.document` outright with a stub of their own
 *  (`shell/draft-run.test.ts`, `apps/claude/shots/native-capture.test.ts`), so
 *  a body attached at import time can be gone by the time a test renders. */
export function installPortalContainer(): void {
  const doc = (globalThis as { document?: { body?: unknown } }).document;
  if (!doc) return;
  const body = doc.body as { nodeType?: number } | undefined;
  if (body && body.nodeType === 1) return;
  doc.body = {
    nodeType: 1,
    children: [],
    createNodeMock: () => ({
      querySelector: () => null,
      querySelectorAll: () => [],
      contains: () => false,
      focus() {},
      addEventListener() {},
      removeEventListener() {},
    }),
    // The ordinary DOM members a few modules reach for on the body (a drag
    // class, a temporary <a> for a download). No-ops: they neither throw nor
    // pretend to have done anything.
    classList: { add() {}, remove() {}, contains: () => false },
    appendChild: (child: unknown) => child,
    removeChild: (child: unknown) => child,
    setAttribute() {},
    removeAttribute() {},
    contains: () => false,
  };
}

/** Undo `installPortalContainer` — call it once the suite's dialogs are
 *  unmounted, so the next file in the run sees the `document` it would have
 *  seen without this one. */
export function removePortalContainer(): void {
  const doc = (globalThis as { document?: { body?: unknown } }).document;
  if (!doc) return;
  delete doc.body;
}
