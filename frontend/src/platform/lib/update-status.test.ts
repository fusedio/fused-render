// Pure logic only — `updateRelevant` and `updateLabel` are what the badge,
// the collapsed rail's dot, and the Settings popover row each gate/word
// themselves on, so a bug here would be wrong in three places at once.
import { describe, expect, it } from "bun:test";
import type { UpdateStatus } from "@platform/lib/api";
import { updateLabel, updateRelevant } from "./update-status";

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
