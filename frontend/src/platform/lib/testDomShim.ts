// A handful of suites import modules that read `window`, `location`, or
// `history` at MODULE SCOPE (router.ts's legacy-path rewrite and IS_EMBED,
// appShot.ts's pointerdown listener) because that is genuinely when those
// modules need to know their environment — a page load, not a function call.
// Bun's test runtime has no DOM, so those reads need something on
// `globalThis` before the import happens.
//
// This shim is installed TWICE over, and both halves matter:
//
//  1. `bunfig.toml`'s `[test] preload` runs `testDomShim.preload.ts` before
//     the runner loads the first test file, so these globals exist from the
//     start of the process. That is what makes a PLAIN STATIC import of a
//     module with module-scope DOM reads safe — a static import is hoisted
//     above every statement in the importing file, so no in-file call can
//     ever run early enough to cover it. Four files (ActivityDock,
//     StatusBar, DownloadManager, appSeed) import router.ts exactly that way
//     and, before the preload existed, passed only because some OTHER file
//     happened to load first and leave `location` standing: each one failed
//     on its own (`bun test <that file>`) and the whole run failed whenever
//     the order shifted — which a merge that only ADDS test files is enough
//     to do.
//  2. An explicit `installDomShim()` at the top of a file, before a
//     `await import()` of the module under test. Redundant with the preload
//     now, and kept: it is the line that says out loud why the import below
//     it is dynamic, and it keeps the file runnable if the preload is ever
//     dropped.
//
// Every suite shares one `globalThis` and bun does not reset globals between
// files in one run, so two rules hold:
//
//   * Extend the objects HERE, in this one place, when a module needs one
//     more member off `window`/`location`/`history` — never re-add a
//     competing stub in a test file. Whoever installs first wins for the
//     whole process, so a per-file stub carrying only the members THAT file
//     needs hands every later file a global that is truthy and half-missing.
//   * Never `delete` one of these three globals. A suite that needs its own
//     `window` (the listing's virtual `Clock`) or its own `history` (a
//     nav-capturing `pushState`) SAVES the value it is replacing and puts it
//     back through `restoreGlobal` — a teardown that deletes instead is what
//     re-opens case 1 for every file that runs after it, and for the next
//     module evaluation of anything not yet imported. Measured both ways: a
//     delete at file top level and a delete inside a test body each break a
//     later file's router.ts evaluation.
//
// `??=`, not `=`: the preload wins, and a suite that calls this after
// installing something richer keeps what it installed.
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
    dispatchEvent: () => true,
    addEventListener() {},
    removeEventListener() {},
    setTimeout: globalThis.setTimeout.bind(globalThis),
    clearTimeout: globalThis.clearTimeout.bind(globalThis),
    setInterval: globalThis.setInterval.bind(globalThis),
    clearInterval: globalThis.clearInterval.bind(globalThis),
  };
}

/**
 * Put back a global a suite displaced, the counterpart to saving it first.
 *
 * `globalThis.x = saved` on its own would leave `undefined` STANDING where
 * the global had been genuinely absent (`document`, which this shim does not
 * install), which reads differently to a `typeof` guard than a missing
 * binding does. So an absent original is restored by deleting, and everything
 * else by assignment — never a bare `delete` in a teardown, which is what
 * strands the files that import a module with module-scope DOM reads.
 */
export function restoreGlobal(name: string, saved: unknown): void {
  const g = globalThis as Record<string, unknown>;
  if (saved === undefined) delete g[name];
  else g[name] = saved;
}
