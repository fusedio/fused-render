import { describe, expect, it } from "bun:test";
import { ageLabel, matchCell, matchRowTip, matchTitle, poolBuildBanner, popLabel, quantLabel, splitRepoId, verdictGlyph } from "./hubTableView";
import type { AiFitVerdict, HubMatchAxis } from "@platform/lib/api";

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

describe("matchRowTip", () => {
  // D1245/D1246: the row's own short `data-tip` popover — unlike `matchTitle`
  // above (a 60-word paragraph), this leads with the score then names ONLY
  // the axes that actually cost THIS row points, biggest loss first, and
  // always separates "the score" from "the colour" in plain words — the
  // fix for the "two rows both score 84 but one is yellow and one is
  // green" complaint (D1246).
  const tight = (footprintGb: number, poolGb: number): AiFitVerdict => ({
    verdict: "tight",
    basis: "declared",
    footprintBytes: footprintGb * 1e9,
    score: 60,
    runMode: "gpu",
  });

  const axis = (partial: Partial<HubMatchAxis> & Pick<HubMatchAxis, "axis" | "gained" | "lost">): HubMatchAxis =>
    partial as HubMatchAxis;

  it("leads with the score out of 100", () => {
    const tip = matchRowTip(tight(17, 22.4), 84, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
    ]);
    expect(tip.startsWith("Match 84/100")).toBe(true);
  });

  it("names the biggest losers first, capped at three, each with its own number", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
      axis({ axis: "popularity", gained: 0.5, lost: 9.5, downloads: 120 }),
      axis({ axis: "recency", gained: 4.5, lost: 10.5, ageDays: 1095 }),
      axis({ axis: "speed", gained: 15, lost: 0, tokensPerSecond: 40 }),
      axis({ axis: "capability", gained: 25, lost: 0, params: 8_000_000_000 }),
    ];
    const tip = matchRowTip(tight(17, 22.4), 84, breakdown);
    // Sorted by `lost` descending (14, 10.5, 9.5): fit, recency, popularity —
    // all three fit within the top-three cap, so all three are named.
    expect(tip).toContain("popularity (120 downloads)");
    expect(tip).toContain("3y"); // recency phrased as an age
    // Full-marks axes never named.
    expect(tip).not.toContain("speed");
    expect(tip).not.toContain("capability");
  });

  it("says nothing lost when every axis scored full marks", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 2, poolGb: 22.4 }),
      axis({ axis: "capability", gained: 25, lost: 0, params: 8_000_000_000 }),
      axis({ axis: "speed", gained: 15, lost: 0, tokensPerSecond: 40 }),
      axis({ axis: "recency", gained: 15, lost: 0, ageDays: 1 }),
      axis({ axis: "popularity", gained: 10, lost: 0, downloads: 6_000_000 }),
    ];
    const tip = matchRowTip(tight(2, 22.4), 100, breakdown);
    expect(tip).not.toContain("lost");
  });

  it("mentions the already-downloaded bonus when it applied", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
      axis({ axis: "onDisk", gained: 6, lost: 0 }),
    ];
    const tip = matchRowTip(tight(17, 22.4), 84, breakdown);
    expect(tip.toLowerCase()).toContain("already");
    expect(tip).toContain("+6");
  });

  it("blames unknown size, not unused capacity, when capability lost points with no params", () => {
    // C5 (Bugbot): a row with no declared params still uses the capability
    // axis's default score, but the phrase must not invent "this machine
    // could run more" reasoning next to a dash it never measured.
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 21, lost: 0, footprintGb: 17, poolGb: 22.4 }),
      axis({ axis: "capability", gained: 0, lost: 25, params: null }),
    ];
    const tip = matchRowTip(tight(17, 22.4), 84, breakdown);
    expect(tip.toLowerCase()).toContain("size unknown");
    expect(tip.toLowerCase()).not.toContain("could run more");
  });

  it("names a run-mode penalty as its own loss when it applied", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 2, poolGb: 22.4 }),
      axis({ axis: "runMode", gained: 0, lost: 10, runMode: "cpu-offload" }),
    ];
    const tip = matchRowTip(tight(2, 22.4), 90, breakdown);
    expect(tip.toLowerCase()).toContain("cpu offload");
  });

  it("always separates colour from score, and gives the fit its own numbers", () => {
    const tip = matchRowTip(tight(17, 22.4), 84, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
    ]);
    expect(tip.toLowerCase()).toContain("colour");
    expect(tip.toLowerCase()).toContain("not this score");
    expect(tip).toContain("17");
    expect(tip).toContain("22.4");
  });

  it("explains the same colour sentence even when fit itself lost no points", () => {
    // Two rows can both score 84 with different colours precisely because
    // fit is independent of the total — the colour sentence must appear
    // regardless of whether fit made the top-three loss list.
    const easy: AiFitVerdict = { verdict: "easy", basis: "declared", footprintBytes: 8.7e9, score: 100, runMode: "gpu" };
    const tip = matchRowTip(easy, 84, [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 8.7, poolGb: 22.4 }),
      axis({ axis: "popularity", gained: 0, lost: 10, downloads: 0 }),
    ]);
    expect(tip.toLowerCase()).toContain("colour");
    expect(tip).toContain("8.7");
  });

  it("reads unknown fit honestly rather than inventing a footprint", () => {
    const tip = matchRowTip(null, 40, []);
    expect(tip.toLowerCase()).toContain("unknown");
  });

  it("is a dash-safe, terse fallback when there is no breakdown at all", () => {
    const tip = matchRowTip(tight(17, 22.4), 84, undefined);
    expect(tip.startsWith("Match 84/100")).toBe(true);
    expect(tip.toLowerCase()).toContain("colour");
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

describe("verdictGlyph", () => {
  it("maps each fit verdict to the mockup's own glyph", () => {
    expect(verdictGlyph("easy")).toBe("●");
    expect(verdictGlyph("tight")).toBe("▲");
    expect(verdictGlyph("no")).toBe("■");
  });

  it("is the question mark for a verdict this repo never got", () => {
    expect(verdictGlyph("unknown")).toBe("?");
  });
});

describe("poolBuildBanner", () => {
  const now = 1_700_000_000_000;

  it("is null when the pool is ready", () => {
    expect(poolBuildBanner("ready", null, null, now)).toBeNull();
  });

  it("is null when there is no pool at all", () => {
    expect(poolBuildBanner("none", null, null, now)).toBeNull();
  });

  it("is null when poolState is undefined (older API response)", () => {
    expect(poolBuildBanner(undefined, null, null, now)).toBeNull();
  });

  it("reports pages built so far while building", () => {
    expect(poolBuildBanner("building", 4, null, now)).toBe(
      "Building the full catalog for this capability (4 pages so far)… showing live Hub results until it finishes.",
    );
  });

  it("singularizes 'page' for exactly one page done", () => {
    expect(poolBuildBanner("building", 1, null, now)).toBe(
      "Building the full catalog for this capability (1 page so far)… showing live Hub results until it finishes.",
    );
  });

  it("treats a missing pagesDone as zero while building", () => {
    expect(poolBuildBanner("building", null, null, now)).toBe(
      "Building the full catalog for this capability (0 pages so far)… showing live Hub results until it finishes.",
    );
  });

  it("reports a short countdown when blocked", () => {
    const blockedUntil = now / 1000 + 45; // epoch seconds, 45s out
    expect(poolBuildBanner("blocked", null, blockedUntil, now)).toBe(
      "Hub rate limit hit; the full catalog resumes after 45s. Showing live results.",
    );
  });

  it("falls back to 'shortly' when blockedUntil is missing or already past", () => {
    expect(poolBuildBanner("blocked", null, null, now)).toBe(
      "Hub rate limit hit; the full catalog resumes after shortly. Showing live results.",
    );
    const past = now / 1000 - 10;
    expect(poolBuildBanner("blocked", null, past, now)).toBe(
      "Hub rate limit hit; the full catalog resumes after shortly. Showing live results.",
    );
  });
});

