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
  };
}
