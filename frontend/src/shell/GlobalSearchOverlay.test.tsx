// `displayText`'s own "~/" rebasing rule (SPEC-index-plugins.md Part 3).
// Pure function, exported purely for this suite — no render, no network,
// the same split ActivityDock.tsx's `retiredEngines` uses.
import { expect, test } from "bun:test";

// GlobalSearchOverlay.tsx imports router.ts (`navigate`/`navigateUrl`), whose
// module-init `rewriteLegacyPath` reads `location` at IMPORT time (RepoUpdates
// Dock.test.tsx's own header documents the same requirement) — bun's test
// runtime has no DOM, so this has to land before the import below.
(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };
(globalThis as Record<string, unknown>).window = {
  parent: undefined,
  top: undefined,
  dispatchEvent: () => true,
};
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: () => {},
  pushState: () => {},
};

// A dynamic import, AFTER the globals above are installed: a static import
// is hoisted ahead of this file's own top-level code, which would run
// router.ts's module-init before `location` existed (AppPage.test.tsx and
// JobRow.test.tsx use this same dynamic-import workaround for the same
// reason).
const { displayText } = await import("@shell/GlobalSearchOverlay");
import type { IndexRankHit } from "@platform/lib/api";

function hit(rel: string): IndexRankHit {
  return { rel, is_dir: false, size: null, mtime: null };
}

const HOME = "/Users/me";

test("a hit whose base IS the home directory is rebased to ~/", () => {
  expect(displayText(hit("proj/a.ts"), HOME, HOME)).toBe("~/proj/a.ts");
});

// Review finding 8: `displayText` used to label ANY truthy `base` as "~/...",
// so a hit under a mount (or any other configured root that is not the home
// directory) was mislabeled as if it lived under the user's home.
test("a hit under a mount-rooted base (not home) is NOT mislabeled ~/", () => {
  expect(displayText(hit("proj/a.ts"), "/Volumes/External", HOME)).toBe("proj/a.ts");
});

test("an empty base (falsy, but still not home) is also left unrebased", () => {
  expect(displayText(hit("proj/a.ts"), "", HOME)).toBe("proj/a.ts");
});
