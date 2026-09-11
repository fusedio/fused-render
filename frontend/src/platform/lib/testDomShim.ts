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
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
    // The same class as the global, and it has to be the SAME one: Base UI's
    // `isHTMLElement` tests `value instanceof getWindow(value).HTMLElement`,
    // which throws outright — "right hand side of instanceof is not an object" —
    // when the window it reaches for has no such member.
    Element: g.Element,
    HTMLElement: g.HTMLElement,
    requestAnimationFrame: g.requestAnimationFrame,
    cancelAnimationFrame: g.cancelAnimationFrame,
  };
}
