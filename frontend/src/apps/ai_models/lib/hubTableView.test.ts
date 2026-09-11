import { describe, expect, it } from "bun:test";
import { ageLabel, matchCell, matchTitle, popLabel, quantLabel, splitRepoId } from "./hubTableView";
import type { AiFitVerdict } from "@platform/lib/api";

// Every cell rule the search screen draws a value from, tested as a pure
// function — a wrong number reads as a real measurement, so every cell whose
// source can be absent has its dash pinned here directly, per the plan's own
// warning about llmfit's own `search` table (columns filled entirely with
// "-").

describe("ageLabel", () => {
  it("reads a real created date as a compact age", () => {
    const created = new Date(Date.now() - 18 * 86400 * 1000).toISOString();
    expect(ageLabel(created)).toBe("18d ago");
  });

  it("is a dash, never an invented age, when the Hub did not say", () => {
    expect(ageLabel(null)).toBe("—");
  });

  it("is a dash rather than a NaN for an unparseable date", () => {
    expect(ageLabel("not a date")).toBe("—");
  });
});

describe("matchCell", () => {
  const verdict = (v: AiFitVerdict["verdict"], runMode?: AiFitVerdict["runMode"]): AiFitVerdict => ({
    verdict: v,
    basis: "download",
    footprintBytes: 1e9,
    score: 0,
    runMode,
  });

  it("prints the COMPOSITE score, not the memory-only fit score", () => {
    // D780/D781: the merged cell's number is `matchScore`, and the verdict
    // object's own `score` (memory-only) never leaks into either field —
    // that would silently un-merge the two facts this cell exists to keep
    // together but distinct.
    expect(matchCell(verdict("easy"), 87.6)).toEqual({
      scoreText: "88",
      verdict: "easy",
      offloadLabel: null,
    });
  });

  it("colours the number by the MEMORY verdict regardless of the score", () => {
    expect(matchCell(verdict("tight"), 91).verdict).toBe("tight");
    expect(matchCell(verdict("no"), 91).verdict).toBe("no");
  });

  it("is the neutral 'unknown' verdict — not 'no' — for a row with no fit verdict at all", () => {
    // "no" means JUDGED and does not fit; a row nothing could be judged for
    // is a different, honest fourth state.
    expect(matchCell(null, 40).verdict).toBe("unknown");
  });

  it("is a dash, never a bare 0, when there is no matchScore to show", () => {
    expect(matchCell(verdict("easy"), null)).toEqual({
      scoreText: "—",
      verdict: "easy",
      offloadLabel: null,
    });
  });

  it("carries a visible offload suffix for a non-GPU run mode, and none for gpu", () => {
    expect(matchCell(verdict("tight", "cpu-offload"), 50).offloadLabel).toBe("offload");
    expect(matchCell(verdict("tight", "cpu-only"), 50).offloadLabel).toBe("CPU only");
    expect(matchCell(verdict("easy", "gpu"), 50).offloadLabel).toBeNull();
    expect(matchCell(verdict("easy"), 50).offloadLabel).toBeNull();
  });

  it("blanks the number when `stale` — a corrected fit must never sit beside a score computed before it", () => {
    // A GGUF row whose lazy per-file lookup just resolved a real "easy" fit,
    // beside a `matchScore` the server computed against `_FIT_DEFAULT`
    // because `model.fit` was null at scoring time. The verdict still colours
    // the number; the number itself must NOT claim a score for it.
    expect(matchCell(verdict("easy"), 40, true)).toEqual({
      scoreText: "—",
      verdict: "easy",
      offloadLabel: null,
    });
  });
});

describe("matchTitle", () => {
  it("names the axes the composite blends", () => {
    const title = matchTitle({ verdict: "easy", basis: "declared", footprintBytes: 1, score: 100 }, 72);
    expect(title).toContain("Match score 72/100");
    expect(title).toContain("memory fit");
    expect(title).toContain("speed");
    expect(title).toContain("comfortably fits");
  });

  it("says the score is unavailable rather than inventing one", () => {
    expect(matchTitle(null, null)).toContain("unavailable");
  });

  it("folds the run mode in — D782's replacement for the deleted Mode column", () => {
    expect(
      matchTitle({ verdict: "tight", basis: "declared", footprintBytes: 1, runMode: "cpu-offload" }, 40),
    ).toContain("CPU offload");
  });

  it("says the score is not recomputed yet, rather than blending a fit it was not scored against, when stale", () => {
    const title = matchTitle({ verdict: "easy", basis: "declared", footprintBytes: 1, score: 100 }, 40, true);
    expect(title).not.toContain("Match score 40/100");
    expect(title).not.toContain("blends");
    expect(title.toLowerCase()).toContain("not");
  });

  // The `fitBasis` branch is worth its own test: `matchFitBasis` reads the
  // basis straight off `AiFitVerdict.basis` rather than re-deriving a
  // fourth "estimated" state, since no such state exists once derived GGUF
  // fit is out of the picture.
  it("says the fit was measured from a real run, for a 'measured' basis", () => {
    const title = matchTitle({ verdict: "easy", basis: "measured", footprintBytes: 1, score: 100 }, 72, false, "measured");
    expect(title).toContain("measured from real memory usage");
    expect(title).not.toContain("judged from this repo's own reported size");
  });

  it("says the fit was judged from the repo's own reported size, for a 'declared' or 'download' basis", () => {
    const declared = matchTitle({ verdict: "easy", basis: "declared", footprintBytes: 1, score: 100 }, 72, false, "declared");
    const download = matchTitle({ verdict: "easy", basis: "download", footprintBytes: 1, score: 100 }, 72, false, "download");
    expect(declared).toContain("judged from this repo's own reported size");
    expect(download).toContain("judged from this repo's own reported size");
    expect(declared).not.toContain("measured from real memory usage");
  });

  it("adds no basis sentence at all when there is no basis to report", () => {
    const title = matchTitle(null, null, false, null);
    expect(title).not.toContain("This fit is");
  });
});

describe("quantLabel", () => {
  it("renders a measured quant as-is", () => {
    expect(quantLabel("BF16")).toBe("BF16");
    expect(quantLabel("Q4_K_M")).toBe("Q4_K_M");
  });

  it("is a dash rather than a guess when nothing measured it", () => {
    expect(quantLabel(null)).toBe("—");
  });
});

describe("popLabel", () => {
  it("compacts a download count the same way the rest of the page does", () => {
    expect(popLabel(117_000)).toBe("117K");
    expect(popLabel(54_321)).toBe("54K");
    expect(popLabel(42)).toBe("42");
  });

  it("is a dash, never a 0, for a repo the Hub reported no count for", () => {
    expect(popLabel(null)).toBe("—");
  });
});

describe("splitRepoId", () => {
  it("splits an owned id into owner and name", () => {
    expect(splitRepoId("black-forest-labs/FLUX.2-klein-4B")).toEqual({
      owner: "black-forest-labs",
      name: "FLUX.2-klein-4B",
    });
  });

  it("has no owner for a bare id with no slash", () => {
    expect(splitRepoId("gpt2")).toEqual({ owner: null, name: "gpt2" });
  });

  it("splits on the LAST slash when an id has more than one", () => {
    expect(splitRepoId("mlx-community/nested/Qwen3-8B")).toEqual({
      owner: "mlx-community/nested",
      name: "Qwen3-8B",
    });
  });
});

