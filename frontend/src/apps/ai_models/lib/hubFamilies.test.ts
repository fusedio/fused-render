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
    format: null,
    file: null,
    quant: null,
    local: { state: "none" },
    url: `https://huggingface.co/${id}`,
    matchScore: 50,
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

  it("heads a family with its base model, whatever the scores say", () => {
    // Identity, not ranking: the 4-bit republish fits this machine and the
    // fp16 original does not, and the original is still the model the other
    // row is a conversion OF. Drawing the conversion as the family's own
    // name is the thing this rule exists to prevent.
    const rows = [
      model("org/fp16-original", { downloads: 500, fit: fitScore(0) }),
      model("org/4bit-quant", {
        baseModel: "org/fp16-original", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].primary.id).toBe("org/fp16-original");
    expect(families[0].variants.map((m) => m.id)).toEqual(["org/4bit-quant"]);
  });

  it("picks the best-FITTING member as primary when the base itself is absent", () => {
    // With no base repo in these results SOMETHING has to stand in for the
    // family, and the most-downloaded republish is often an fp16 mirror
    // nothing here can load comfortably. Ranking by fit is what promotes the
    // conversion that actually runs, and that has to survive grouping.
    const rows = [
      model("org/fp16-mirror", {
        baseModel: "org/never-seen", relation: "finetune",
        downloads: 500, fit: fitScore(0),
      }),
      model("org/4bit-quant", {
        baseModel: "org/never-seen", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].base).toBeNull();
    expect(families[0].primary.id).toBe("org/4bit-quant");
    expect(families[0].variants.map((m) => m.id)).toEqual(["org/fp16-mirror"]);
  });

  it("falls back to downloads when no member has a fit score", () => {
    const rows = [
      model("org/mirror", { baseModel: "org/never-seen", relation: "finetune", downloads: 10 }),
      model("org/variant", { baseModel: "org/never-seen", relation: "quantized", downloads: 90 }),
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
      // Appears first, but LOSES the primary pick to its sibling below — an
      // fp16 mirror a 4bit quant of the same absent base outranks on fit.
      model("org/base-fp16", {
        baseModel: "org/never-seen", relation: "finetune",
        downloads: 500, fit: fitScore(0),
      }),
      model("org/other", { downloads: 1 }),
      // The best-fitting member of that family — the row actually drawn for
      // it — appears LAST in the input.
      model("org/base-4bit", {
        baseModel: "org/never-seen", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    // The family's primary ("org/base-4bit") sits at index 2 in `rows`, so
    // its family belongs AFTER "org/other" (index 1) — not before it, where
    // first-appearance would have put it.
    expect(families.map((f) => f.key)).toEqual(["org/other", "org/never-seen"]);
    expect(families[1].primary.id).toBe("org/base-4bit");
  });

  it("picks the primary by the ACTIVE sort, not always by fit", () => {
    // A: the better FIT, but the worse MATCH. B: the better MATCH, but the
    // worse fit. Under `sort=best` (the default), the composite matchScore
    // is what the page is actually sorted by, so B — the better match —
    // must be drawn and must sit at its own (earlier) index; under
    // `sort=fit`, A — the better fit — wins instead, exactly as before.
    const rows = [
      model("org/b-better-match", {
        baseModel: "org/base", relation: "quantized",
        fit: fitScore(90), matchScore: 85,
      }),
      model("org/a-better-fit", {
        baseModel: "org/base", relation: "quantized",
        fit: fitScore(100), matchScore: 50,
      }),
    ];
    const best = groupIntoFamilies(rows, "best");
    expect(best[0].primary.id).toBe("org/b-better-match");
    expect(best[0].variants.map((m) => m.id)).toEqual(["org/a-better-fit"]);

    const fit = groupIntoFamilies(rows, "fit");
    expect(fit[0].primary.id).toBe("org/a-better-fit");
    expect(fit[0].variants.map((m) => m.id)).toEqual(["org/b-better-match"]);
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

  it("a GGUF republish gets its own family rather than joining the safetensors one", () => {
    // D676. Both declare the same base, so keying on `baseModel` alone would
    // put them in one bucket — where the GGUF row can never win the primary
    // slot, because a GGUF row carries no fit, no size and no speed (see
    // `hub_models.py::_model_row`) and so blends its `matchScore` from
    // missing-evidence constants. That leaves it permanently behind the
    // expander, which for the one format a llama.cpp user is searching for
    // is the same as being absent.
    const rows = [
      model("Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic", {
        baseModel: "black-forest-labs/FLUX.2-klein-4B", relation: "quantized",
        fit: fitScore(90), matchScore: 74,
      }),
      model("leejet/FLUX.2-klein-4B-GGUF", {
        baseModel: "black-forest-labs/FLUX.2-klein-4B", relation: "quantized",
        format: "gguf", matchScore: 52,
      }),
    ];
    const families = groupIntoFamilies(rows, "best");
    expect(families.map((f) => f.primary.id)).toEqual([
      "Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic",
      "leejet/FLUX.2-klein-4B-GGUF",
    ]);
    // Neither swallowed the other, so neither has variants to disclose.
    expect(families.every((f) => f.variants.length === 0)).toBe(true);
  });

  it("two GGUF republishes of one base still collapse together", () => {
    // The format split is not a licence to stop collapsing: two people's
    // GGUF conversions of the same weights are exactly the redundancy this
    // module exists to fold away, and they share a runner and a Quant
    // column. Only a FORMAT switch — which decides what can open the repo
    // at all — earns a row of its own.
    const rows = [
      model("wikeeyang/Flux2-Klein-9B-True-V3", {
        baseModel: "black-forest-labs/FLUX.2-klein-9B", relation: "quantized",
        format: "gguf", matchScore: 63,
      }),
      model("leejet/FLUX.2-klein-9B-GGUF", {
        baseModel: "black-forest-labs/FLUX.2-klein-9B", relation: "quantized",
        format: "gguf", matchScore: 59,
      }),
    ];
    const families = groupIntoFamilies(rows, "best");
    expect(families).toHaveLength(1);
    expect(families[0].key).toBe("black-forest-labs/FLUX.2-klein-9B gguf");
    expect(families[0].variants.map((m) => m.id)).toEqual(["leejet/FLUX.2-klein-9B-GGUF"]);
  });

  it("sets base to the member that IS the base model, when it's among the results", () => {
    // The base repo itself showed up in this page of results, so it both
    // heads the family and answers `base` — two facts the table reads
    // separately, since `base` stays null for a family whose base is only
    // named by its members' tags.
    const rows = [
      model("org/fp16-original", { downloads: 500, fit: fitScore(0) }),
      model("org/4bit-quant", {
        baseModel: "org/fp16-original", relation: "quantized",
        downloads: 50, fit: fitScore(100),
      }),
    ];
    const families = groupIntoFamilies(rows);
    expect(families[0].baseModel).toBe("org/fp16-original");
    expect(families[0].base?.id).toBe("org/fp16-original");
    expect(families[0].primary.id).toBe("org/fp16-original");
    expect(families[0].variants.map((m) => m.id)).toEqual(["org/4bit-quant"]);
  });

  it("sets baseModel with a null base when the base repo isn't in these results", () => {
    // Same case the "never appeared" test above covers for `key` — the base
    // is named but absent, so the family still knows WHICH repo it's a
    // family of even though that repo isn't a member of it.
    const rows = [model("org/only-the-quant", { baseModel: "org/never-seen", relation: "quantized" })];
    const families = groupIntoFamilies(rows);
    expect(families[0].baseModel).toBe("org/never-seen");
    expect(families[0].base).toBeNull();
  });

  it("leaves both baseModel and base null for a standalone untagged row", () => {
    // No `base_model:` tag at all — the bucket falls back to the row's own
    // id for `key`, and there is no base identity to report either.
    const rows = [model("org/alone", { downloads: 5 })];
    const families = groupIntoFamilies(rows);
    expect(families[0].baseModel).toBeNull();
    expect(families[0].base).toBeNull();
  });

  it("reports the bare base id in baseModel on both sides of a format-keyed split", () => {
    // D676 splits the safetensors and GGUF republishes of one base into two
    // families with different `key`s ("...FLUX.2-klein-4B" vs "...FLUX.2-
    // klein-4B gguf") — `baseModel` is the raw tag underneath that suffix,
    // so both families have to report the SAME bare id despite the split.
    const rows = [
      model("Disty0/FLUX.2-klein-4B-SDNQ-4bit-dynamic", {
        baseModel: "black-forest-labs/FLUX.2-klein-4B", relation: "quantized",
        fit: fitScore(90), matchScore: 74,
      }),
      model("leejet/FLUX.2-klein-4B-GGUF", {
        baseModel: "black-forest-labs/FLUX.2-klein-4B", relation: "quantized",
        format: "gguf", matchScore: 52,
      }),
    ];
    const families = groupIntoFamilies(rows, "best");
    expect(families.map((f) => f.baseModel)).toEqual([
      "black-forest-labs/FLUX.2-klein-4B",
      "black-forest-labs/FLUX.2-klein-4B",
    ]);
    // Neither base repo is actually in these results, so both sides report
    // no base member.
    expect(families.every((f) => f.base === null)).toBe(true);
  });
});
