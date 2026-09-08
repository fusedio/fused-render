// Pure logic only — `updateRelevant` and `updateLabel` are what the badge,
// the collapsed rail's dot, and the Settings popover row each gate/word
// themselves on, so a bug here would be wrong in three places at once.
import { describe, expect, it } from "bun:test";
import type { UpdateStatus } from "@platform/lib/api";
import { pollDelay, updateLabel, updateRelevant } from "./update-status";

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
    expect(pollDelay(st("checking"), 0)).toBe(15_000);
    expect(pollDelay(st("idle"), 30_000)).toBe(15_000);
    // …and settles: idle is also the resting state after a check found nothing.
    expect(pollDelay(st("idle"), 120_000)).toBe(60_000);
    expect(pollDelay(st("checking"), 300_000)).toBe(60_000);
    expect(pollDelay(st("available"), 0)).toBe(60_000);
    // No updater at all (dev run): nothing to be quick about.
    expect(pollDelay(null, 0)).toBe(60_000);
  });
});
