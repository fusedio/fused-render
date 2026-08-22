import { describe, expect, it } from "bun:test";
import type { Job } from "@platform/lib/jobs";
import { liveModelTotal, liveSizeOverride, modelSizeHint, modelSizeLabel } from "./modelSize";

// A card used to show the catalog's approximate constant BESIDE the job row's
// own total — `~64 GB` next to `68 GB / 68 GB`, one download, two numbers, and
// the only reading available to the person looking at it is that the download
// is overrunning its own size. These tests pin which number wins, and just as
// importantly the four cases where the constant is still the only one there is.

function job(extra: Partial<Job> = {}): Job {
  return {
    id: "sys:ai-model:org-m",
    title: "org/m",
    detail: "Fetching weights…",
    kind: "download",
    state: "running",
    done: 1_000,
    total: 73_014_444_032, // 68 GB
    unit: "bytes",
    message: "",
    page: "",
    owner: "server",
    cancellable: true,
    cancel_requested: false,
    started_at: 0,
    updated_at: 0,
    finished_at: null,
    stalled: false,
    ...extra,
  };
}

describe("modelSizeLabel", () => {
  it("prefers a running download's own total over the catalog's constant", () => {
    // The whole point: the fetcher summed this from the live listing for exactly
    // the files it is pulling, and the constant is hand-written and stale.
    expect(modelSizeLabel(64, job())).toBe("68 GB");
  });

  it("falls back to the catalog figure when the row has no total yet", () => {
    // Every phase that does not know its size reports null — a venv build, a
    // weight load, a download still listing the repo.
    expect(modelSizeLabel(64, job({ total: null }))).toBe("64 GB");
  });

  it("ignores a total of zero, which is not a size", () => {
    expect(modelSizeLabel(64, job({ total: 0 }))).toBe("64 GB");
  });

  it("ignores a finished row, whose total measured a pull that is over", () => {
    expect(modelSizeLabel(64, job({ state: "done" }))).toBe("64 GB");
  });

  it("ignores a cancelled row, which never measured the whole repo", () => {
    expect(modelSizeLabel(64, job({ state: "cancelled" }))).toBe("64 GB");
  });

  it("ignores a row whose total is not bytes at all", () => {
    // `total` counts steps on an image job and seconds on a transcription;
    // formatted as bytes, 16 steps would read "16 B".
    expect(modelSizeLabel(64, job({ unit: "s", total: 16 }))).toBe("64 GB");
  });

  it("falls back to the catalog figure when there is no job", () => {
    expect(modelSizeLabel(64, undefined)).toBe("64 GB");
  });

  it("shows the em-dash when nobody has recorded a size and nothing is running", () => {
    expect(modelSizeLabel(null, undefined)).toBe("—");
  });

  it("still shows a live total for a model the catalog never measured", () => {
    expect(modelSizeLabel(null, job())).toBe("68 GB");
  });
});

describe("modelSizeHint", () => {
  it("is null when there is nothing to say, so the phrase can be left out", () => {
    expect(modelSizeHint(null, undefined)).toBeNull();
  });

  it("marks the catalog's constant as approximate and the live total as not", () => {
    // "~68 GB" over a measured figure would keep the hedge the live number
    // exists to remove.
    expect(modelSizeHint(64, undefined)).toEqual({ text: "64 GB", approx: true });
    expect(modelSizeHint(64, job())).toEqual({ text: "68 GB", approx: false });
  });
});

describe("liveSizeOverride", () => {
  it("is null when there is no live total, so a Hub-measured size survives", () => {
    // The search cards' own figure comes from the Hub's dtype map or the repo's
    // total (`hubSize.ts`) — a third number, and the right one until a pull of
    // this repo starts reporting its own.
    expect(liveSizeOverride(undefined)).toBeNull();
    expect(liveSizeOverride(job({ total: null }))).toBeNull();
  });

  it("replaces it, and says why, once the fetcher reports a total", () => {
    const over = liveSizeOverride(job());
    expect(over?.text).toBe("68 GB");
    expect(over?.title).toContain("actually fetching");
  });
});

describe("liveModelTotal", () => {
  it("is null for every row that is not a running byte count", () => {
    expect(liveModelTotal(undefined)).toBeNull();
    expect(liveModelTotal(job({ state: "error" }))).toBeNull();
    expect(liveModelTotal(job())).toBe(73_014_444_032);
  });
});
