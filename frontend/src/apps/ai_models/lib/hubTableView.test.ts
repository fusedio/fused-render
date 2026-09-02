import { describe, expect, it } from "bun:test";
import {
  ageLabel,
  capabilityHint,
  familyDisplay,
  familyHoist,
  columnVisible,
  isMajorityValue,
  isMatchScoreStale,
  hoistSummary,
  hoistValue,
  majorityValue,
  matchCell,
  matchFitBasis,
  matchTitle,
  popLabel,
  quantLabel,
  resolveFit,
  resolveSpeed,
  speedLabel,
  speedTitle,
  variantLabel,
} from "./hubTableView";
import type { AiFitVerdict, AiSpeedEstimate, HubModel } from "@platform/lib/api";
import type { HubFamily } from "./hubFamilies";

// Every cell rule the dense table draws a column from, tested as a pure
// function — the density that makes this table worth having is also what
// makes a wrong number louder, so every cell whose source can be absent has
// its dash pinned here directly, per the plan's own warning about llmfit's
// own `search` table (columns filled entirely with "-").

function model(id: string, extra: Partial<HubModel> = {}): HubModel {
  return {
    id,
    task: "text generation",
    taskHelp: null,
    pipelineTag: "text-generation",
    capability: "text-generation",
    gated: null,
    library: null,
    downloads: null,
    likes: null,
    updated: null,
    params: null,
    estimatedSize: null,
    fit: null,
    speedEstimate: null,
    created: null,
    baseModel: null,
    relation: null,
    quant: null,
    file: null,
    local: { state: "none" },
    url: `https://huggingface.co/${id}`,
    matchScore: 50,
    ...extra,
  };
}

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

  it("bars and prints the COMPOSITE score, not the memory-only fit score", () => {
    // D639/D640: the merged cell's number is `matchScore`, and the verdict
    // object's own `score` (memory-only) never leaks into either field —
    // that would silently un-merge the two facts this cell exists to keep
    // together but distinct.
    expect(matchCell(verdict("easy"), 87.6)).toEqual({
      percent: 87.6,
      scoreText: "88",
      dot: "easy",
      offloadLabel: null,
    });
  });

  it("colours (and shapes) the dot by the MEMORY verdict regardless of the score", () => {
    expect(matchCell(verdict("tight"), 91).dot).toBe("tight");
    expect(matchCell(verdict("no"), 91).dot).toBe("no");
  });

  it("is the neutral 'unknown' dot — not 'no' — for a row with no fit verdict at all", () => {
    // "no" means JUDGED and does not fit; a row nothing could be judged for
    // is a different, honest fourth state.
    expect(matchCell(null, 40).dot).toBe("unknown");
  });

  it("bars at 0 with a dash, never a bare 0, when there is no matchScore to show", () => {
    expect(matchCell(verdict("easy"), null)).toEqual({
      percent: 0,
      scoreText: "—",
      dot: "easy",
      offloadLabel: null,
    });
  });

  it("carries a visible offload suffix for a non-GPU run mode, and none for gpu", () => {
    expect(matchCell(verdict("tight", "cpu-offload"), 50).offloadLabel).toBe("offload");
    expect(matchCell(verdict("tight", "cpu-only"), 50).offloadLabel).toBe("CPU only");
    expect(matchCell(verdict("easy", "gpu"), 50).offloadLabel).toBeNull();
    expect(matchCell(verdict("easy"), 50).offloadLabel).toBeNull();
  });

  it("blanks the bar/number when `stale` — a corrected fit must never sit beside a score computed before it (code review finding)", () => {
    // A GGUF row whose lazy per-file lookup just resolved a real "easy" fit,
    // beside a `matchScore` the server computed against `_FIT_DEFAULT`
    // because `model.fit` was null at scoring time. The dot still shows the
    // real verdict; the bar/number must NOT claim a number for it.
    expect(matchCell(verdict("easy"), 40, true)).toEqual({
      percent: 0,
      scoreText: "—",
      dot: "easy",
      offloadLabel: null,
    });
  });
});

describe("resolveFit / resolveSpeed", () => {
  const verdict = (v: AiFitVerdict["verdict"]): AiFitVerdict => ({
    verdict: v,
    basis: "download",
    footprintBytes: 1e9,
    score: 0,
  });

  it("a resolved override wins when the lookup could actually judge (file !== null)", () => {
    const stale = verdict("easy");
    const measured = verdict("tight");
    expect(resolveFit(stale, measured, "x-Q4_K_M.gguf")).toBe(measured);
  });

  it("a resolved-to-null override still wins when file !== null — 'nothing to judge' is itself an answer", () => {
    const stale = verdict("easy");
    expect(resolveFit(stale, null, "x-Q4_K_M.gguf")).toBeNull();
  });

  it("does NOT let a never-judges null override wipe a real modelFit, when file === null", () => {
    // The bug this fix pins: `api_hub_size` only ever computes a fit verdict
    // when it was asked with a `file`. A row with `model.file === null` still
    // runs the lazy lookup (for the repo-wide total), which always answers
    // `fit: null` — not because there was nothing to judge, but because that
    // request shape never judges at all. `lookupTotalSize` caches that `null`
    // indistinguishably from a real "asked, and there was nothing to judge"
    // answer (`hubSize.test.ts:295`), so without gating on `file`, this would
    // wipe a real `basis: "measured"` verdict a model already on disk earned
    // at search time.
    const measured = verdict("easy");
    expect(resolveFit(measured, null, null)).toBe(measured);
    expect(resolveFit(measured, verdict("tight"), null)).toBe(measured);
  });

  it("falls back to model.fit when the lookup has not answered yet (undefined)", () => {
    const guess = verdict("easy");
    expect(resolveFit(guess, undefined, "x-Q4_K_M.gguf")).toBe(guess);
  });

  it("is null, never undefined, when neither side has anything", () => {
    expect(resolveFit(null, undefined, "x-Q4_K_M.gguf")).toBeNull();
    expect(resolveFit(null, undefined, null)).toBeNull();
  });

  it("resolveSpeed follows the identical file-gated precedence", () => {
    const speed = (tokensPerSecond: number): AiSpeedEstimate => ({
      tokensPerSecond,
      method: "backend-constant",
      backend: "cpu-x86",
      bandwidthGbS: null,
      contextTokens: 8192,
      calibrated: false,
      calibrationFactor: null,
    });
    const modelSpeed = speed(5);
    const measuredSpeed = speed(40);
    expect(resolveSpeed(modelSpeed, measuredSpeed, "x-Q4_K_M.gguf")).toBe(measuredSpeed);
    expect(resolveSpeed(modelSpeed, undefined, "x-Q4_K_M.gguf")).toBe(modelSpeed);
    expect(resolveSpeed(modelSpeed, null, "x-Q4_K_M.gguf")).toBeNull();
    expect(resolveSpeed(modelSpeed, null, null)).toBe(modelSpeed);
  });
});

describe("matchFitBasis", () => {
  const verdict = (v: AiFitVerdict["verdict"], basis: AiFitVerdict["basis"] = "download"): AiFitVerdict => ({
    verdict: v,
    basis,
    footprintBytes: 1e9,
    score: 0,
  });

  it("is null when there is no fit to show at all", () => {
    expect(matchFitBasis(null)).toBeNull();
  });

  it("reads the basis straight off the verdict's own wire field", () => {
    expect(matchFitBasis(verdict("easy", "measured"))).toBe("measured");
    expect(matchFitBasis(verdict("easy", "declared"))).toBe("declared");
    expect(matchFitBasis(verdict("easy", "download"))).toBe("download");
  });
});

describe("isMatchScoreStale", () => {
  const verdict = (v: AiFitVerdict["verdict"]): AiFitVerdict => ({
    verdict: v,
    basis: "download",
    footprintBytes: 1e9,
    score: 0,
  });

  it("is true when a null model.fit is corrected by a REAL resolved fit", () => {
    expect(isMatchScoreStale(null, verdict("easy"))).toBe(true);
  });

  it("is false when the lookup resolves to null — nothing was corrected (code review finding 3)", () => {
    // `knownFit`'s own pinned contract (`hubSize.test.ts`): a lookup that
    // resolved with nothing to judge answers `null`, not `undefined`. That
    // is not a correction of the server's `_FIT_DEFAULT`-scored matchScore
    // — nothing case-relevant changed — so the Match cell must not blank a
    // perfectly valid score.
    expect(isMatchScoreStale(null, null)).toBe(false);
  });

  it("is false while the lookup has not answered yet", () => {
    expect(isMatchScoreStale(null, undefined)).toBe(false);
  });

  it("is true when a non-null model.fit is corrected to a DIFFERING verdict, not just when it was null", () => {
    // The gap the fifth fix-builder round flagged and left open: comparing
    // only nullness missed a real correction that happened to start from a
    // non-null `model.fit`. With derived GGUF fit deleted entirely, a
    // `file !== null` row's `model.fit` is now always null (see
    // `resolveFit`'s own doc), so this path is not reachable from a GGUF row
    // any more — but the function itself must still not silently pass a
    // genuine mismatch, for any future caller shape.
    expect(isMatchScoreStale(verdict("easy"), verdict("tight"))).toBe(true);
  });

  it("is false when the resolved fit agrees with model.fit — nothing to correct", () => {
    const same = verdict("easy");
    expect(isMatchScoreStale(same, verdict("easy"))).toBe(false);
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

  it("folds the run mode in — D641's replacement for the deleted Mode column", () => {
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

  // The `fitBasis` branch (code review finding 2) had no test before this
  // round — added alongside the fix that made `matchFitBasis` read the basis
  // straight off `AiFitVerdict.basis` instead of re-deriving a fourth
  // "estimated" state that no longer exists once derived GGUF fit was
  // deleted.
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

describe("speedLabel", () => {
  const estimate = (tokensPerSecond: number): AiSpeedEstimate => ({
    tokensPerSecond,
    method: "bandwidth",
    backend: "metal-mlx",
    bandwidthGbS: 200,
    contextTokens: 8192,
    calibrated: false,
    calibrationFactor: null,
  });
  const BILLION = 1_000_000_000;

  it("shows one decimal of tok/s under 10", () => {
    expect(speedLabel(estimate(2.13), 7 * BILLION)).toBe("2.1");
  });

  it("shows a plain rounded integer from 10 up to 999", () => {
    expect(speedLabel(estimate(24.13), 7 * BILLION)).toBe("24");
    expect(speedLabel(estimate(342.9), 7 * BILLION)).toBe("343");
  });

  it("compacts anything at or past 1000 with one decimal and a 'k' — never a raw 5-digit figure", () => {
    expect(speedLabel(estimate(1200), 7 * BILLION)).toBe("1.2k");
    expect(speedLabel(estimate(1000), 7 * BILLION)).toBe("1.0k");
  });

  it("is a dash below the formula's own validated anchor — the bug this fixes", () => {
    // The exact repro: `tiny-Qwen2ForCausalLM-2.5`, 2M parameters, printing a
    // confident "17.3k" — a number `speed.py:283`'s own documented anchor
    // rule (`params_b >= 1.0`) says the formula has no business producing.
    expect(speedLabel(estimate(17_324.6), 2_000_000)).toBe("—");
  });

  it("is NOT dashed at or above the one-billion-parameter anchor", () => {
    expect(speedLabel(estimate(24), BILLION)).toBe("24");
  });

  it("is a dash, never '0 tok/s', when there is no estimate", () => {
    expect(speedLabel(null, 7 * BILLION)).toBe("—");
  });

  it("is a dash when params is unknown too — no anchor to check, and no estimate either way", () => {
    expect(speedLabel(null, null)).toBe("—");
  });

  it("is a dash for a non-finite figure rather than a formatting crash", () => {
    expect(speedLabel(estimate(Number.NaN), 7 * BILLION)).toBe("—");
    expect(speedLabel(estimate(Number.POSITIVE_INFINITY), 7 * BILLION)).toBe("—");
  });
});

describe("speedTitle", () => {
  it("names the anchor rule below one billion parameters", () => {
    expect(speedTitle(2_000_000)).toContain("one billion parameters");
  });

  it("is undefined at or above the anchor, and for unknown params", () => {
    expect(speedTitle(1_000_000_000)).toBeUndefined();
    expect(speedTitle(null)).toBeUndefined();
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

describe("variantLabel", () => {
  it("states the count once there is more than one repo in the family", () => {
    expect(variantLabel(4)).toBe("4");
    expect(variantLabel(31)).toBe("31");
  });

  it("is a dash for a family that is just the one repo — never a bare 0", () => {
    expect(variantLabel(0)).toBe("—");
  });
});

describe("familyDisplay", () => {
  function family(primary: HubModel, variants: HubModel[] = []): HubFamily {
    return { key: primary.baseModel ?? primary.id, primary, variants };
  }

  it("names the row by the primary's OWN id — the repo href and Download both act on", () => {
    // The base model is the more "readable" name, but it is not necessarily
    // a repo this app can run at all (D313/hubFamilies' own "never appeared"
    // case), and two different base models can share a repo name — so the
    // bold name must be the thing a click on this row actually reaches.
    const primary = model("mlx-community/Qwen3.8-27B-4bit", { baseModel: "Qwen/Qwen3.8-27B" });
    const display = familyDisplay(family(primary, [model("x")]));
    expect(display.name).toBe("mlx-community/Qwen3.8-27B-4bit");
    expect(display.baseModel).toBe("Qwen/Qwen3.8-27B");
  });

  it("carries no base model for a standalone repo with none", () => {
    const primary = model("tencent/Hy4-preview");
    const display = familyDisplay(family(primary));
    expect(display.name).toBe("tencent/Hy4-preview");
    expect(display.baseModel).toBeNull();
  });
});

describe("capabilityHint", () => {
  it("is undefined when the Hub's task label and the capability slug agree", () => {
    // The common case this column collapse (D641) exists for: both strings
    // read "text-generation" (or "text generation"/"text-generation" before
    // the merge) — the same fact stated twice with nothing to disclose.
    expect(capabilityHint({ task: "text-generation", capability: "text-generation" })).toBeUndefined();
  });

  it("names the Hub's own task label when it genuinely differs from the capability", () => {
    const hint = capabilityHint({ task: "image-classification", capability: "image-embedding" });
    expect(hint).toContain("image-classification");
  });

  it("is undefined when the Hub gave no task label at all", () => {
    expect(capabilityHint({ task: null, capability: "text-generation" })).toBeUndefined();
  });
});

describe("hoistValue", () => {
  it("is the shared value when every row agrees — the only case it returns non-null", () => {
    expect(hoistValue(["a", "a", "a"])).toEqual({ value: "a" });
  });

  it("is null for anything short of unanimous, even a strong majority", () => {
    // 4 of 5 = 80%, the old (now-retired) majority floor — no longer enough.
    expect(hoistValue(["a", "a", "a", "a", "b"])).toBeNull();
    // 3 of 4 = 75%, below even that old floor.
    expect(hoistValue(["a", "a", "a", "b"])).toBeNull();
  });

  it("a single null anywhere breaks unanimity outright, even with every known value agreeing", () => {
    expect(hoistValue(["a", "a", "a", "a", null])).toBeNull();
    expect(hoistValue([null, null, null])).toBeNull();
  });

  it("is null for an empty result set", () => {
    expect(hoistValue([])).toBeNull();
  });
});

describe("majorityValue", () => {
  it("is the modal value at or above the 80% floor — the styling-only concept `hoistValue` used to double as", () => {
    expect(majorityValue(["a", "a", "a", "a", "b"])).toEqual({ value: "a" });
  });

  it("is null just under the floor", () => {
    expect(majorityValue(["a", "a", "a", "b"])).toBeNull();
  });

  it("nulls count toward the denominator but never toward the modal count", () => {
    expect(majorityValue(["a", "a", "a", "a", null])).toEqual({ value: "a" });
    expect(majorityValue([null, null, null])).toBeNull();
  });

  it("is also non-null for a fully unanimous set (the column just won't be visible for it to matter)", () => {
    expect(majorityValue(["a", "a", "a"])).toEqual({ value: "a" });
  });

  it("is null for an empty result set", () => {
    expect(majorityValue([])).toBeNull();
  });
});

describe("columnVisible", () => {
  it("is absent (false) when the hoist is non-null — unanimous by construction", () => {
    expect(columnVisible({ value: "BF16" }, ["BF16", "BF16", "BF16"])).toBe(false);
  });

  it("is present (true) with no hoist — a majority short of unanimous, or real diversity", () => {
    expect(columnVisible(null, ["BF16", "BF16", "BF16", "BF16", "Q4_K_M"])).toBe(true);
    expect(columnVisible(null, ["BF16", "Q4_K_M", "Q8_0", "F16"])).toBe(true);
  });

  it("is absent when NOTHING is known — a column of dashes states nothing", () => {
    expect(columnVisible(null, [null, null, null])).toBe(false);
  });

  it("is absent for an empty result set", () => {
    expect(columnVisible(null, [])).toBe(false);
  });
});

describe("isMajorityValue", () => {
  it("is true for a row matching the majority value", () => {
    expect(isMajorityValue("BF16", { value: "BF16" })).toBe(true);
  });

  it("is false for a row that does not match it", () => {
    expect(isMajorityValue("Q4_K_M", { value: "BF16" })).toBe(false);
  });

  it("is false with no majority, and false for a null (unknown) value", () => {
    expect(isMajorityValue("BF16", null)).toBe(false);
    expect(isMajorityValue(null, { value: "BF16" })).toBe(false);
  });
});

describe("hoistSummary", () => {
  it("states the unanimous capability and the count together", () => {
    expect(hoistSummary(21, { value: "text-generation" }, null)).toBe("21 text-generation models");
  });

  it("states nothing about a column that did not reach unanimity — no hoist means no clause, never 'mostly'", () => {
    expect(hoistSummary(5, null, null)).toBe("5 models");
  });

  it("appends the quant hoist after the capability one, both stated as 'all'-strength facts", () => {
    expect(hoistSummary(21, { value: "text-generation" }, { value: "BF16" })).toBe(
      "21 text-generation models · all BF16",
    );
  });

  it("uses the singular noun for exactly one model", () => {
    expect(hoistSummary(1, null, null)).toBe("1 model");
  });

  it("is null for an empty result set even with a hoist to report", () => {
    expect(hoistSummary(0, { value: "text-generation" }, null)).toBeNull();
  });
});

describe("familyHoist", () => {
  // Code review finding 4 (presence/summary must share one value set), and
  // D661 (unanimity-only hoisting): four states pinned directly, the
  // variant-dominated shape (few primaries, many hidden variants) explicitly
  // among them since it is the shape that regressed twice.
  function family(id: string, quant: string | null, variantQuants: (string | null)[] = []): HubFamily {
    const primary = model(id, { quant });
    const variants = variantQuants.map((q, i) => model(`${id}-variant-${i}`, { quant: q }));
    return { key: id, primary, variants };
  }

  it("unanimous across primaries AND variants: hides the column, summary says 'all'", () => {
    const families = [family("a", "BF16"), family("b", "BF16", ["BF16"]), family("c", "BF16")];
    const { quantHoist, quantMajority, summary, showQuant } = familyHoist(families);
    expect(quantHoist).toEqual({ value: "BF16" });
    expect(quantMajority).toEqual({ value: "BF16" });
    expect(showQuant).toBe(false);
    expect(summary).toContain("all BF16");
  });

  it("majority with a differing VARIANT (the regressed shape): three visible primaries all BF16, fifteen hidden variants Q4_K_M — column stays, summary claims nothing about quant", () => {
    // Round 2's own contradiction: a primaries-only hoist (or a majority
    // hoist over the full set) would have let "mostly Q4_K_M" print above
    // three visible cells that all read BF16. Now: any disagreement in the
    // full set (primaries+variants) means no hoist at all, so the summary
    // states no quant fact — but the column still renders, and the three
    // BF16 primaries are each muted (`quantMajority`) since Q4_K_M is now
    // the actual majority value across the full 18-row set.
    const families = [
      family("a", "BF16", Array(5).fill("Q4_K_M")),
      family("b", "BF16", Array(5).fill("Q4_K_M")),
      family("c", "BF16", Array(5).fill("Q4_K_M")),
    ];
    const { quantHoist, quantMajority, summary, showQuant } = familyHoist(families);
    expect(quantHoist).toBeNull();
    expect(quantMajority).toEqual({ value: "Q4_K_M" });
    expect(showQuant).toBe(true);
    expect(summary).not.toContain("BF16");
    expect(summary).not.toContain("Q4_K_M");
    expect(summary).not.toContain("mostly");
  });

  it("majority with a differing PRIMARY: four primaries BF16, one primary Q4_K_M (80%) — column stays, no quant clause, BF16 primaries muted", () => {
    const families = [
      family("a", "BF16"),
      family("b", "BF16"),
      family("c", "BF16"),
      family("d", "BF16"),
      family("e", "Q4_K_M"),
    ];
    const { quantHoist, quantMajority, summary, showQuant } = familyHoist(families);
    expect(quantHoist).toBeNull();
    expect(quantMajority).toEqual({ value: "BF16" });
    expect(showQuant).toBe(true);
    expect(summary).not.toContain("mostly");
  });

  it("one null among otherwise-agreeing knowns: not unanimous, column stays, no quant clause", () => {
    // A row that genuinely does not know is exactly when the column earns
    // its place — nulls must never be filtered out to manufacture agreement.
    const families = [family("a", "BF16"), family("b", "BF16"), family("c", null)];
    const { quantHoist, summary, showQuant } = familyHoist(families);
    expect(quantHoist).toBeNull();
    expect(showQuant).toBe(true);
    expect(summary).not.toContain("BF16");
  });

  it("all-unknown: no hoist, no summary clause, and the column stays visible (real diversity of nothing known)", () => {
    const families = [family("a", null), family("b", null, [null])];
    const { quantHoist, summary, showQuant } = familyHoist(families);
    expect(quantHoist).toBeNull();
    // No quant clause at all when nothing is known — every fixture here
    // shares `model()`'s default `capability: "text-generation"`, so the
    // capability hoist is unanimous and the summary states only that. The
    // count is `allRows.length` (D661's denominator fix), not
    // `families.length` — here 2 families with 1 hidden null variant makes
    // 3 total rows, all still `text-generation`.
    const totalRows = families.flatMap((f) => [f.primary, ...f.variants]).length;
    expect(summary).toBe(`${totalRows} text-generation models`);
    // A column of nothing but dashes is dropped, per `columnVisible`'s own
    // "NOTHING is known at all" case.
    expect(showQuant).toBe(false);
  });
});
