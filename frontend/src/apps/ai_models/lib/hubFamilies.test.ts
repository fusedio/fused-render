import { describe, expect, it } from "bun:test";
import { groupIntoFamilies } from "./hubFamilies";
import type { HubModel } from "@platform/lib/api";

// A quant/finetune republish is a fact about the SAME model shown twice (or a
// dozen times) on a recency-sorted search — this is the rule that collapses
// them back to one row, tested directly since the JSX that draws it is not.

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
    file: null,
    quant: null,
    local: { state: "none" },
    url: `https://huggingface.co/${id}`,
    ...extra,
  };
}

function fitScore(score: number): HubModel["fit"] {
  return { verdict: score >= 100 ? "easy" : score > 0 ? "tight" : "no",
           basis: "download", footprintBytes: 1e9, score };
}

describe("groupIntoFamilies", () => {
  it("groups every relation the Hub tags a republish with", () => {
    // quantized, finetune, merge, adapter — all four collapse under the same
    // base model, because the frontend's rule does not narrow to a subset:
    // the MLX ports on this machine mostly declare `finetune`, not
    // `quantized`, and keying on one relation alone would split them apart.
    const rows = [
      model("org/base", { downloads: 100 }),
      model("org/base-quantized", { baseModel: "org/base", relation: "quantized", downloads: 40 }),
      model("org/base-finetune", { baseModel: "org/base", relation: "finetune", downloads: 30 }),
      model("org/base-merge", { baseModel: "org/base", relation: "merge", downloads: 20 }),
      model("org/base-adapter", { baseModel: "org/base", relation: "adapter", downloads: 10 }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families).toHaveLength(1);
    expect(families[0].primary.id).toBe("org/base");
    expect(families[0].variants.map((m) => m.id).sort()).toEqual([
      "org/base-adapter",
      "org/base-finetune",
      "org/base-merge",
      "org/base-quantized",
    ]);
  });

  it("gives an untagged row its own family", () => {
    const rows = [model("org/alone", { downloads: 5 })];
    const families = groupIntoFamilies(rows);
    expect(families).toHaveLength(1);
    expect(families[0].primary.id).toBe("org/alone");
    expect(families[0].variants).toEqual([]);
  });

  it("picks the best-FITTING member as primary, not merely the most-downloaded", () => {
    // The most-downloaded republish is often an fp16 original nothing here
    // can load comfortably; the point of ranking by fit at all is promoting
    // the runnable conversion, and that has to survive grouping.
    const rows = [
      model("org/fp16-original", { downloads: 500, fit: fitScore(0) }),
      model("org/4bit-quant", {
        baseModel: "org/fp16-original", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].primary.id).toBe("org/4bit-quant");
    expect(families[0].variants.map((m) => m.id)).toEqual(["org/fp16-original"]);
  });

  it("falls back to downloads when no member has a fit score", () => {
    const rows = [
      model("org/base", { downloads: 10 }),
      model("org/variant", { baseModel: "org/base", relation: "quantized", downloads: 90 }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].primary.id).toBe("org/variant");
  });

  it("keeps the server's own ranking as the tie-break, stably", () => {
    // Two members tied on fit AND downloads (both null/absent, say) must not
    // be shuffled — the server's ranking is the only ordering information
    // left, and this is what makes it survive grouping.
    const rows = [
      model("org/base", { downloads: null }),
      model("org/second", { baseModel: "org/base", relation: "quantized", downloads: null }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].primary.id).toBe("org/base");
    expect(families[0].variants.map((m) => m.id)).toEqual(["org/second"]);
  });

  it("preserves the order families first appear in, across repeated runs", () => {
    const rows = [
      model("org/z", { downloads: 1 }),
      model("org/a", { downloads: 1 }),
      model("org/z-quant", { baseModel: "org/z", relation: "quantized", downloads: 1 }),
    ];
    const families = groupIntoFamilies(rows);
    // "org/z"'s family appears first (it was first in the input), then the
    // standalone "org/a" — never resorted by anything this module invented.
    expect(families.map((f) => f.key)).toEqual(["org/z", "org/a"]);
  });

  it("places a family at its PRIMARY's index, not at whichever member appeared first", () => {
    // `models` arrives pre-sorted (e.g. by size ascending). A family draws
    // its primary's row for every column, so it has to sit at the primary's
    // position in that order or a sort-visible column (Size, Pop.) stops
    // looking sorted the moment a family's first-appearing member isn't its
    // primary.
    const rows = [
      // Appears first, but LOSES the primary pick to its own variant below —
      // this is the fp16 original a 4bit quant of it outranks on fit.
      model("org/base", { downloads: 500, fit: fitScore(0) }),
      model("org/other", { downloads: 1 }),
      // The best-fitting member of "org/base"'s family — the row actually
      // drawn for that family — appears LAST in the input.
      model("org/base-4bit", {
        baseModel: "org/base", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    // The family's primary ("org/base-4bit") sits at index 2 in `rows`, so
    // its family belongs AFTER "org/other" (index 1) — not before it, where
    // first-appearance would have put it.
    expect(families.map((f) => f.key)).toEqual(["org/other", "org/base"]);
    expect(families[1].primary.id).toBe("org/base-4bit");
  });

  it("a variant whose base model never appeared in these results still groups under it", () => {
    // The base repo may not have matched the query or may have been dropped
    // upstream (D313) — the variant still names it, and still deserves a
    // family of its own rather than standing alone under its OWN id.
    const rows = [model("org/only-the-quant", { baseModel: "org/never-seen", relation: "quantized" })];
    const families = groupIntoFamilies(rows);
    expect(families).toHaveLength(1);
    expect(families[0].key).toBe("org/never-seen");
    expect(families[0].primary.id).toBe("org/only-the-quant");
    expect(families[0].variants).toEqual([]);
  });
});
