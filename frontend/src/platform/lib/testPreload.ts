// `bunfig.toml`'s `[test] preload` entry — bun runs this once, before ANY
// test file's own module code, for the whole `bun test` process. That is
// what actually closes the ordering bug `testDomShim.ts`'s own header
// describes: calling `installDomShim()` at the top of a test file only
// protects suites whose OWN file runs before the first suite that transitively
// imports something reading `window`/`location`/`history` at module scope
// (router.ts's `IS_EMBED`, appShot.ts's pointerdown listener). Bun does not
// guarantee file execution order — CI (ubuntu-latest) and a local run
// (macOS) enumerate `bun test`'s target directory differently, `--randomize`
// exists precisely because order is unspecified, and a handful of small
// explicit file subsets (e.g. `bun test src/platform/lib/explain-with-ai.test.ts
// src/platform/ui/JobPopupCard.test.tsx`) reliably hit exactly this ordering
// gap locally — the first file's `installDomShim()` call was never reached
// before the second file's transitive `import "@platform/lib/router"`
// evaluated router.ts's own module-scope `location` read, and it threw
// `ReferenceError: location is not defined` before a single test in either
// file could run, reported as an "Unhandled error between tests" for BOTH.
//
// A preload has no such race: bun evaluates it before opening the first test
// file at all, so every suite's very first import already finds
// `window`/`location`/`history` on `globalThis`, in every order bun ever
// picks. Test files keep their own `installDomShim()` calls (documented
// there as harmless — it is `??=`-based, so a call here first makes every
// later one a no-op) rather than losing that self-documentation; this file
// is the actual fix, they are now redundant belt-and-braces.
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
