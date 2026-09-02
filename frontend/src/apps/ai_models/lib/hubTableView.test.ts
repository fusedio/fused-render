import { describe, expect, it } from "bun:test";
import {
  ageLabel,
  capabilityHint,
  familyDisplay,
  columnVisible,
  isMajorityValue,
  hoistSummary,
  hoistValue,
  matchCell,
  matchTitle,
  popLabel,
  quantLabel,
  runModeLabel,
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

describe("runModeLabel", () => {
  it("names the three run modes in words a reader chose, not the wire strings", () => {
    expect(runModeLabel("gpu")).toBe("GPU");
    expect(runModeLabel("cpu-offload")).toBe("CPU offload");
    expect(runModeLabel("cpu-only")).toBe("CPU only");
  });

  it("is a dash when there is no fit to read a run mode off of", () => {
    expect(runModeLabel(undefined)).toBe("—");
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
  it("is unanimous when every row shares one value", () => {
    expect(hoistValue(["a", "a", "a"])).toEqual({ value: "a", unanimous: true });
  });

  it("hoists the modal value, not unanimous, at or above the 80% majority", () => {
    // 4 of 5 = 80%, the documented floor.
    expect(hoistValue(["a", "a", "a", "a", "b"])).toEqual({ value: "a", unanimous: false });
  });

  it("hoists nothing just under the majority threshold", () => {
    // 3 of 4 = 75%, below 80%.
    expect(hoistValue(["a", "a", "a", "b"])).toBeNull();
  });

  it("never counts a null as agreement, and nulls still count toward the denominator", () => {
    // 4 of 5 known values agree, but the fifth (null) still counts against
    // the total — 4/5 = 80% still clears it here, but the null must not be
    // silently dropped from the count entirely.
    expect(hoistValue(["a", "a", "a", "a", null])).toEqual({ value: "a", unanimous: false });
    expect(hoistValue([null, null, null])).toBeNull();
  });

  it("is null for an empty result set", () => {
    expect(hoistValue([])).toBeNull();
  });
});

describe("columnVisible", () => {
  it("is absent (false) when every row agrees — the unanimous case", () => {
    expect(columnVisible({ value: "BF16", unanimous: true }, ["BF16", "BF16", "BF16"])).toBe(false);
  });

  it("is present (true) under a majority hoist, so the minority stays legible", () => {
    expect(columnVisible({ value: "BF16", unanimous: false }, ["BF16", "BF16", "BF16", "BF16", "Q4_K_M"])).toBe(
      true,
    );
  });

  it("is present when there is no hoist at all — real diversity, below the majority floor", () => {
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
  it("is true for a row matching a non-unanimous hoist's own value", () => {
    expect(isMajorityValue("BF16", { value: "BF16", unanimous: false })).toBe(true);
  });

  it("is false for the minority row under that same hoist", () => {
    expect(isMajorityValue("Q4_K_M", { value: "BF16", unanimous: false })).toBe(false);
  });

  it("is false under a UNANIMOUS hoist — that column is not rendered at all, so styling is moot", () => {
    expect(isMajorityValue("BF16", { value: "BF16", unanimous: true })).toBe(false);
  });

  it("is false with no hoist, and false for a null (unknown) value", () => {
    expect(isMajorityValue("BF16", null)).toBe(false);
    expect(isMajorityValue(null, { value: "BF16", unanimous: false })).toBe(false);
  });
});

describe("hoistSummary", () => {
  it("states the unanimous capability and the count together", () => {
    expect(hoistSummary(21, { value: "text-generation", unanimous: true }, null)).toBe(
      "21 text-generation models",
    );
  });

  it("says 'mostly' for a majority-only capability hoist", () => {
    expect(hoistSummary(5, { value: "text-generation", unanimous: false }, null)).toBe(
      "5 models (mostly text-generation)",
    );
  });

  it("appends the quant hoist after the capability one", () => {
    expect(
      hoistSummary(21, { value: "text-generation", unanimous: true }, { value: "BF16", unanimous: false }),
    ).toBe("21 text-generation models · mostly BF16");
  });

  it("uses the singular noun for exactly one model", () => {
    expect(hoistSummary(1, null, null)).toBe("1 model");
  });

  it("is null for an empty result set even with a hoist to report", () => {
    expect(hoistSummary(0, { value: "text-generation", unanimous: true }, null)).toBeNull();
  });
});
