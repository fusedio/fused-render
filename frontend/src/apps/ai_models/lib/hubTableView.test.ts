import { describe, expect, it } from "bun:test";
import { ageLabel, downloadedVariantLabel, matchCell, matchRowTip, matchScoreInt, matchTitle, nextPoolPhase, pagesFetchedLabel, poolBuildBanner, popLabel, quantLabel, splitRepoId, variantIsDownloadable, verdictGlyph } from "./hubTableView";
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

describe("matchScoreInt", () => {
  it("is the one rounding rule both the cell and the tooltip read from", () => {
    // Item 4: a 84.5 `matchScore` must never print "85" in the cell and
    // "84" in the tooltip — one helper, one answer.
    expect(matchScoreInt(84.5)).toBe(85);
    expect(matchScoreInt(84.4)).toBe(84);
    expect(matchScoreInt(null)).toBeNull();
    expect(matchScoreInt(undefined)).toBeNull();
  });
});

describe("matchRowTip", () => {
  // D1245/D1246, cut down by D1267: the row's own short `data-tip` popover —
  // two lines, max ~90 characters total. Line 1 names at most the two
  // biggest-loss axes (no parentheticals, no raw numbers); line 2 always
  // states the fit verdict plus its GB numbers, rounded to one decimal —
  // the fix for two rows that both show "84" with a different bar colour
  // reading as a bug (D1246) rather than two independent facts.
  const tight = (footprintGb: number, poolGb: number): AiFitVerdict => ({
    verdict: "tight",
    basis: "declared",
    footprintBytes: footprintGb * 1e9,
    score: 60,
    runMode: "gpu",
  });

  const axis = (partial: Partial<HubMatchAxis> & Pick<HubMatchAxis, "axis" | "gained" | "lost">): HubMatchAxis =>
    partial as HubMatchAxis;

  it("leads with the plain integer score, not a slash-100 paragraph", () => {
    const tip = matchRowTip(tight(17, 22.4), 84, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
    ]);
    expect(tip.startsWith("Match 84")).toBe(true);
    expect(tip).not.toContain("/100");
  });

  it("agrees with matchCell's rounding — item 4's 84.5 case", () => {
    const tip = matchRowTip(tight(17, 22.4), 84.5, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
    ]);
    expect(matchCell(tight(17, 22.4), 84.5).scoreText).toBe("85");
    expect(tip.startsWith("Match 85")).toBe(true);
  });

  it("names the two biggest losers, biggest first, and drops the rest", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 17, poolGb: 22.4 }),
      axis({ axis: "popularity", gained: 0.5, lost: 9.5, downloads: 120 }),
      axis({ axis: "recency", gained: 4.5, lost: 10.5, ageDays: 1095 }),
      axis({ axis: "speed", gained: 15, lost: 0, tokensPerSecond: 40 }),
      axis({ axis: "capability", gained: 25, lost: 0, params: 8_000_000_000 }),
    ];
    const tip = matchRowTip(tight(17, 22.4), 84, breakdown);
    // `fit` is never in the loss list (line 2 already tells its whole
    // story). Of the rest, sorted by `lost` descending: recency (10.5),
    // popularity (9.5) — capped at two, so only those two are named, and
    // never with parentheticals or raw numbers.
    expect(tip).toContain("lost most on recency, then popularity");
    expect(tip).not.toContain("(");
    expect(tip).not.toContain("120");
    expect(tip).not.toContain("speed");
    expect(tip).not.toContain("size");
  });

  it("says full marks when every scoreable axis lost nothing", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 2, poolGb: 22.4 }),
      axis({ axis: "capability", gained: 25, lost: 0, params: 8_000_000_000 }),
      axis({ axis: "speed", gained: 15, lost: 0, tokensPerSecond: 40 }),
      axis({ axis: "recency", gained: 15, lost: 0, ageDays: 1 }),
      axis({ axis: "popularity", gained: 10, lost: 0, downloads: 6_000_000 }),
    ];
    const tip = matchRowTip({ verdict: "easy", basis: "declared", footprintBytes: 2e9, score: 100 }, 100, breakdown);
    expect(tip).toContain("full marks");
  });

  it("names a run-mode penalty as a loss when it applied", () => {
    const breakdown: HubMatchAxis[] = [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 2, poolGb: 22.4 }),
      axis({ axis: "runMode", gained: 0, lost: 10, runMode: "cpu-offload" }),
    ];
    const tip = matchRowTip({ verdict: "easy", basis: "declared", footprintBytes: 2e9, score: 100 }, 90, breakdown);
    expect(tip).toContain("lost most on offload");
  });

  it("rounds the fit GB numbers to one decimal on a tight-fit row", () => {
    const tip = matchRowTip(tight(18.663, 26.359), 84, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 18.663, poolGb: 26.359 }),
    ]);
    expect(tip).toContain("Tight fit · needs ~18.7 of 26.4 GB");
  });

  it("reads 'Fits easily' with both GB numbers for an easy verdict", () => {
    const easy: AiFitVerdict = { verdict: "easy", basis: "declared", footprintBytes: 9.2e9, score: 100 };
    const tip = matchRowTip(easy, 84, [
      axis({ axis: "fit", gained: 35, lost: 0, footprintGb: 9.2, poolGb: 26.4 }),
    ]);
    expect(tip).toContain("Fits easily · 9.2 of 26.4 GB");
  });

  it("reads \"Won't fit\" with just the footprint for a no-fit verdict", () => {
    const no: AiFitVerdict = { verdict: "no", basis: "declared", footprintBytes: 41e9, score: 0 };
    const tip = matchRowTip(no, 30, [axis({ axis: "fit", gained: 0, lost: 35, footprintGb: 41, poolGb: 26.4 })]);
    expect(tip).toContain("Won't fit · needs 41 GB");
  });

  it("reads unknown fit honestly rather than inventing a footprint", () => {
    const tip = matchRowTip(null, 40, []);
    expect(tip).toContain("Memory not measured");
  });

  it("is a dash-safe, terse fallback when there is no breakdown at all", () => {
    const tip = matchRowTip(tight(17, 22.4), 84, undefined);
    expect(tip.startsWith("Match 84")).toBe(true);
    expect(tip).toContain("Tight fit");
  });

  it("stays within the ~90 character budget", () => {
    const tip = matchRowTip(tight(18.663, 26.359), 84, [
      axis({ axis: "fit", gained: 21, lost: 14, footprintGb: 18.663, poolGb: 26.359 }),
      axis({ axis: "popularity", gained: 0.5, lost: 9.5, downloads: 120 }),
      axis({ axis: "recency", gained: 4.5, lost: 10.5, ageDays: 1095 }),
    ]);
    expect(tip.length).toBeLessThanOrEqual(90);
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

describe("pagesFetchedLabel", () => {
  it("shows a connecting placeholder before any page has landed", () => {
    expect(pagesFetchedLabel(null)).toBe("connecting to the Hub…");
    expect(pagesFetchedLabel(undefined)).toBe("connecting to the Hub…");
    expect(pagesFetchedLabel(0)).toBe("connecting to the Hub…");
  });

  it("singularizes 'page' for exactly one", () => {
    expect(pagesFetchedLabel(1)).toBe("1 page fetched");
  });

  it("pluralizes for more than one", () => {
    expect(pagesFetchedLabel(4)).toBe("4 pages fetched");
  });
});

describe("nextPoolPhase", () => {
  it("goes to building whenever poolState is building, from any phase", () => {
    expect(nextPoolPhase("hidden", "building")).toBe("building");
    expect(nextPoolPhase("building", "building")).toBe("building");
    expect(nextPoolPhase("done", "building")).toBe("building");
  });

  it("only reaches done by leaving building for ready", () => {
    expect(nextPoolPhase("building", "ready")).toBe("done");
  });

  it("never celebrates a pane that opened already ready", () => {
    expect(nextPoolPhase("hidden", "ready")).toBe("hidden");
  });

  it("holds done until the component's timer clears it", () => {
    expect(nextPoolPhase("done", "ready")).toBe("done");
    expect(nextPoolPhase("done", "none")).toBe("done");
    expect(nextPoolPhase("done", undefined)).toBe("done");
  });

  it("is hidden for blocked/none/undefined outside a hold", () => {
    expect(nextPoolPhase("hidden", "blocked")).toBe("hidden");
    expect(nextPoolPhase("hidden", "none")).toBe("hidden");
    expect(nextPoolPhase("hidden", undefined)).toBe("hidden");
    expect(nextPoolPhase("building", "blocked")).toBe("hidden");
  });
});



describe("downloadedVariantLabel", () => {
  it("names the plain quant for the default variant", () => {
    expect(
      downloadedVariantLabel({
        variantCount: 3,
        variants: [
          { file: "model-Q4_K_M.gguf", quant: "Q4_K_M" },
          { file: "model-Q8_0.gguf", quant: "Q8_0" },
        ],
        file: "model-Q4_K_M.gguf",
        quant: "Q4_K_M",
        localFile: "model-Q4_K_M.gguf",
      }),
    ).toBe("Q4_K_M downloaded");
  });

  it("names the variant count and quant when a non-default variant is on disk", () => {
    expect(
      downloadedVariantLabel({
        variantCount: 3,
        variants: [
          { file: "model-Q4_K_M.gguf", quant: "Q4_K_M" },
          { file: "model-Q8_0.gguf", quant: "Q8_0" },
        ],
        file: "model-Q4_K_M.gguf",
        quant: "Q4_K_M",
        localFile: "model-Q8_0.gguf",
      }),
    ).toBe("3 variants · Q8_0 downloaded");
  });

  it("falls back to the plain default caption for a non-GGUF row (no variants at all)", () => {
    expect(
      downloadedVariantLabel({
        variantCount: null,
        variants: null,
        file: null,
        quant: "BF16",
        localFile: null,
      }),
    ).toBe("BF16 downloaded");
  });

  it("reads 'Downloaded' with no quant known at all", () => {
    expect(
      downloadedVariantLabel({
        variantCount: null,
        variants: null,
        file: null,
        quant: null,
        localFile: null,
      }),
    ).toBe("Downloaded");
  });

  it("does not treat a single-variant repo as a non-default download even if file differs", () => {
    // variantCount <= 1 means there is nothing to disambiguate — the plain
    // caption applies regardless of any (theoretical) file mismatch.
    expect(
      downloadedVariantLabel({
        variantCount: 1,
        variants: [{ file: "model.gguf", quant: "Q4_K_M" }],
        file: "model.gguf",
        quant: "Q4_K_M",
        localFile: "model.gguf",
      }),
    ).toBe("Q4_K_M downloaded");
  });

  it("falls back to the row's own quant when the local file isn't in the variant list", () => {
    expect(
      downloadedVariantLabel({
        variantCount: 2,
        variants: [{ file: "model-Q4_K_M.gguf", quant: "Q4_K_M" }],
        file: "model-Q4_K_M.gguf",
        quant: "Q4_K_M",
        localFile: "model-mystery.gguf",
      }),
    ).toBe("2 variants · Q4_K_M downloaded");
  });

  it("names the variant count alone when no quant is known at all", () => {
    expect(
      downloadedVariantLabel({
        variantCount: 2,
        variants: [{ file: "model-Q4_K_M.gguf", quant: null }],
        file: "model-Q4_K_M.gguf",
        quant: null,
        localFile: "model-Q8_0.gguf",
      }),
    ).toBe("2 variants downloaded");
  });
});

describe("variantIsDownloadable", () => {
  it("is downloadable when the server says true", () => {
    expect(variantIsDownloadable({ downloadable: true })).toBe(true);
  });

  it("is not downloadable when the server says false (a sharded quant)", () => {
    expect(variantIsDownloadable({ downloadable: false })).toBe(false);
  });

  it("is downloadable when the field is absent (a cached response predating it)", () => {
    expect(variantIsDownloadable({})).toBe(true);
  });
});
