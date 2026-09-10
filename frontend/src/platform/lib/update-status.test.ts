// Pure logic only — `updateRelevant` and `updateLabel` are what the badge,
// the collapsed rail's dot, and the Settings popover row each gate/word
// themselves on, so a bug here would be wrong in three places at once.
import { describe, expect, it } from "bun:test";
import type { UpdateStatus } from "@platform/lib/api";
import {
  CHECK_RESULT_HOLD_MS,
  checkNowLabel,
  pollDelay,
  shouldCheckOnReturn,
  updateLabel,
  updateRelevant,
} from "./update-status";

function status(overrides: Partial<UpdateStatus>): UpdateStatus {
  return {
    state: "idle",
    method: "dmg",
    latest_version: null,
    progress: null,
    progress_total: null,
    error: null,
    manual_command: null,
    ...overrides,
  };
}

describe("updateRelevant", () => {
  it("is false for null and for idle/checking", () => {
    expect(updateRelevant(null)).toBe(false);
    expect(updateRelevant(status({ state: "idle" }))).toBe(false);
    expect(updateRelevant(status({ state: "checking" }))).toBe(false);
  });

  it("is true for available, installing, installed, and error", () => {
    for (const state of ["available", "installing", "installed", "error"]) {
      expect(updateRelevant(status({ state }))).toBe(true);
    }
  });
});

describe("updateLabel", () => {
  it("names the version once one is available", () => {
    expect(updateLabel(status({ state: "available", latest_version: "0.5.10" }))).toBe(
      "Update available — v0.5.10"
    );
  });

  it("falls back to the bare phrase when no version is known", () => {
    expect(updateLabel(status({ state: "available", latest_version: null }))).toBe(
      "Update available"
    );
  });

  it("says Updating… while installing, regardless of version", () => {
    expect(
      updateLabel(status({ state: "installing", latest_version: "0.5.10" }))
    ).toBe("Updating…");
  });

  it("says Ready to restart once installed", () => {
    expect(updateLabel(status({ state: "installed", latest_version: "0.5.10" }))).toBe(
      "Ready to restart"
    );
  });

  it("falls to the available phrasing for error (still names the version)", () => {
    expect(updateLabel(status({ state: "error", latest_version: "0.5.10" }))).toBe(
      "Update available — v0.5.10"
    );
  });
});


describe("pollDelay", () => {
  const st = (state: UpdateStatus["state"]) => ({ state } as UpdateStatus);
  it("is quick while an install runs, warm only while the first check is still plausibly coming", () => {
    expect(pollDelay(st("installing"), 0)).toBe(2_000);
    // Hot for the first twenty seconds — the server's first check lands ~1s in.
    expect(pollDelay(st("checking"), 0)).toBe(2_000);
    expect(pollDelay(st("idle"), 10_000)).toBe(2_000);
    expect(pollDelay(st("checking"), 20_000)).toBe(15_000);
    expect(pollDelay(st("idle"), 30_000)).toBe(15_000);
    // …and settles: idle is also the resting state after a check found nothing.
    expect(pollDelay(st("idle"), 120_000)).toBe(60_000);
    expect(pollDelay(st("checking"), 300_000)).toBe(60_000);
    expect(pollDelay(st("available"), 0)).toBe(60_000);
    // No updater at all (dev run): nothing to be quick about.
    expect(pollDelay(null, 0)).toBe(60_000);
  });
});

// Check-on-return: the app coming back to the front is what closes the gap
// between a release and the badge, so the three ways this could be wrong get
// pinned here — too eager, a dev run with no updater, and a `focus` that fired
// on a document nobody is looking at.
describe("shouldCheckOnReturn", () => {
  const GAP = 30 * 60_000;
  const idle = { state: "idle" } as UpdateStatus;

  it("checks once the gap has passed", () => {
    expect(shouldCheckOnReturn(0, GAP, idle, true)).toBe(true);
    expect(shouldCheckOnReturn(0, GAP + 1, idle, true)).toBe(true);
  });

  it("stays quiet inside the gap, so cmd-tabbing is not a run of requests", () => {
    expect(shouldCheckOnReturn(0, 0, idle, true)).toBe(false);
    expect(shouldCheckOnReturn(0, GAP - 1, idle, true)).toBe(false);
  });

  it("never checks without an updater — a dev run has none and the POST 404s", () => {
    expect(shouldCheckOnReturn(0, GAP * 10, null, true)).toBe(false);
  });

  it("never checks for a hidden document, however long it has been", () => {
    expect(shouldCheckOnReturn(0, GAP * 10, idle, false)).toBe(false);
  });

});

describe("shouldCheckOnReturn only re-asks from idle", () => {
  it("is false once an update is available, running, installed or failed", () => {
    for (const state of ["available", "installing", "installed", "error", "checking"] as const) {
      expect(shouldCheckOnReturn(0, 3_600_000, status({ state }), true)).toBe(false);
    }
    expect(shouldCheckOnReturn(0, 3_600_000, status({ state: "idle" }), true)).toBe(true);
  });
});

describe("checkNowLabel", () => {
  // The idle row's four phases, worded once here rather than read off a tree.
  it("offers the check at rest and says so while it runs", () => {
    expect(checkNowLabel("rest", "0.5.22")).toBe("Check for updates");
    expect(checkNowLabel("checking", "0.5.22")).toBe("Checking…");
  });

  it("names the version it is current at, when it knows it", () => {
    expect(checkNowLabel("current", "0.5.22")).toBe("Up to date · v0.5.22");
    // /api/config has not answered yet, or an old server without `version`.
    expect(checkNowLabel("current", null)).toBe("Up to date");
    expect(checkNowLabel("current", undefined)).toBe("Up to date");
  });

  it("owns up to a failed check without blaming anything", () => {
    expect(checkNowLabel("failed", "0.5.22")).toBe("Couldn't check");
  });

  it("holds an answer long enough to read, not long enough to look stuck", () => {
    expect(CHECK_RESULT_HOLD_MS).toBeGreaterThanOrEqual(3_000);
    expect(CHECK_RESULT_HOLD_MS).toBeLessThanOrEqual(6_000);
  });
});
