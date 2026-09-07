// Regression coverage for code review findings 2, 3 and 7 (round 3):
// `clearShellSnapshot` is the ONE place every clear-`_snapshot` path now
// routes through, so this is the one place the sidebar hop
// (`_fusedSnapshotCleared`) needs proving — every caller (Preview.tsx's
// `backToLive` and its resolve-effect's 404 branch, `useSnapshotForFolder`'s
// own `backToLive` and 404 branch) is covered by construction once this
// function itself is right.
//
// `document` is faked per-test (this suite's own throwaway object), not
// added to `platform/lib/testDomShim.ts`'s shared shim: no other suite in
// this process touches `document` at all, so a permanent addition there
// would be a needless shared-state risk for a need exactly one file has —
// the same "per-file throwaway global" precedent
// `useSnapshotForFolder.test.ts` already sets for `location`/`history`.
import { afterEach, beforeEach, describe, expect, it } from "bun:test";
import { installDomShim } from "@platform/lib/testDomShim";

installDomShim();
const { setResolvedSnapshot, getResolvedSnapshot } = await import(
  "@platform/lib/snapshot-param"
);
const { clearShellSnapshot } = await import("./snapshot-clear");

type FakeLocation = { search: string; pathname: string };
const curLoc = () => (globalThis as unknown as { location: FakeLocation }).location;

describe("clearShellSnapshot", () => {
  const originalLocation = (globalThis as Record<string, unknown>).location;
  const originalHistory = (globalThis as Record<string, unknown>).history;
  const originalDocument = (globalThis as Record<string, unknown>).document;

  beforeEach(() => {
    setResolvedSnapshot(null);
    (globalThis as Record<string, unknown>).location = {
      search: "?_snapshot=abc1234&foo=bar",
      pathname: "/w/myapp/x.py",
    };
    (globalThis as Record<string, unknown>).history = {
      state: null,
      replaceState: (_state: unknown, _title: string, url: string) => {
        const target = curLoc();
        const qIndex = url.indexOf("?");
        target.pathname = qIndex === -1 ? url : url.slice(0, qIndex);
        target.search = qIndex === -1 ? "" : url.slice(qIndex);
      },
    };
  });

  afterEach(() => {
    (globalThis as Record<string, unknown>).location = originalLocation;
    (globalThis as Record<string, unknown>).history = originalHistory;
    (globalThis as Record<string, unknown>).document = originalDocument;
  });

  it("clears the singleton and drops _snapshot from the URL, keeping other params", () => {
    setResolvedSnapshot({ sha: "abc1234", dir: "/x", app_dir: "/w/myapp" });
    (globalThis as Record<string, unknown>).document = {
      querySelector: () => null,
    };

    clearShellSnapshot();

    expect(getResolvedSnapshot()).toBe(null);
    expect(curLoc().search).toBe("?foo=bar");
  });

  it("THE non-skippable assertion: tells the sidebar via _fusedSnapshotCleared " +
    "when a matching side frame is mounted", () => {
    let notified = false;
    const fakeFrame = {
      contentWindow: {
        _fusedSnapshotCleared: () => {
          notified = true;
        },
      },
    };
    (globalThis as Record<string, unknown>).document = {
      querySelector: (sel: string) =>
        sel === ".preview-side-frame" ? fakeFrame : null,
    };

    clearShellSnapshot();

    expect(notified).toBe(true);
  });

  it("does not throw when no side frame is mounted", () => {
    (globalThis as Record<string, unknown>).document = {
      querySelector: () => null,
    };

    expect(() => clearShellSnapshot()).not.toThrow();
  });

  it("does not throw when the side frame is showing something other than git " +
    "(no _fusedSnapshotCleared export)", () => {
    (globalThis as Record<string, unknown>).document = {
      querySelector: () => ({ contentWindow: {} }),
    };

    expect(() => clearShellSnapshot()).not.toThrow();
  });

  it(
    "FINDING 7: does not throw when reading off the side frame's " +
      "contentWindow throws (a cross-origin or sandboxed frame)",
    () => {
      const fakeFrame = {
        get contentWindow(): never {
          throw new DOMException("Blocked a frame with origin", "SecurityError");
        },
      };
      (globalThis as Record<string, unknown>).document = {
        querySelector: () => fakeFrame,
      };

      // Before the fix, an uncaught SecurityError here would propagate out
      // of `clearShellSnapshot` (called AFTER the URL write), aborting
      // whatever the caller still meant to do afterward.
      expect(() => clearShellSnapshot()).not.toThrow();
      // The rest of the clear (singleton + URL) must still have happened —
      // a thrown hop must not abort the clear itself.
      expect(getResolvedSnapshot()).toBe(null);
      expect(curLoc().search).toBe("?foo=bar");
    }
  );
});
