import { expect, test } from "bun:test";

import type { AiCatalogModel } from "@platform/lib/api";
import {
  USE_CASES,
  alternativeThatSees,
  modelThinks,
  parseUseCase,
  pickForUseCase,
  useCaseById,
  useCasesOf,
} from "./useCases";

// `useCases.ts` imports nothing but a type: Home (eager) reads it for its chip
// labels, so a runtime import of anything heavy here would drag the playground
// into the front-door bundle.

function model(id: string, over: Partial<AiCatalogModel> = {}): AiCatalogModel {
  return {
    id,
    label: id,
    size_gb: 1,
    note: null,
    source: "curated",
    downloaded: false,
    loaded: false,
    recommended: true,
    ...over,
  };
}

test("three use cases, in reading order, each with everything a surface needs", () => {
  expect(USE_CASES.map((u) => u.id)).toEqual(["writing", "coding", "reasoning"]);
  for (const u of USE_CASES) {
    expect(u.label.length).toBeGreaterThan(0);
    expect(u.blurb.length).toBeGreaterThan(0);
    expect(u.placeholder.length).toBeGreaterThan(0);
    expect(u.starters.length).toBeGreaterThanOrEqual(4);
    for (const s of u.starters) {
      expect(s.name && s.hint && s.prompt && s.icon).toBeTruthy();
    }
  }
});

test("thinking defaults: off for writing and coding, on for reasoning", () => {
  expect(USE_CASES.map((u) => u.thinking)).toEqual([false, false, true]);
});

test("starter names are unique across the whole set (they are React keys)", () => {
  const names = USE_CASES.flatMap((u) => u.starters.map((s) => s.name));
  expect(new Set(names).size).toBe(names.length);
});

test("the eight original starters kept their prompts, split by use case", () => {
  const byUse = (id: string) => useCaseById(id as "writing")!.starters.map((s) => s.name);
  expect(byUse("writing")).toEqual(
    expect.arrayContaining(["Decline a meeting", "Dinner from this", "How it guesses"]),
  );
  expect(byUse("coding")).toEqual(expect.arrayContaining(["Explain an error", "Regex, in parts"]));
});

test("parseUseCase accepts only the three ids", () => {
  expect(parseUseCase("coding")).toBe("coding");
  expect(parseUseCase("vision")).toBeNull();
  expect(parseUseCase("")).toBeNull();
  expect(parseUseCase(null)).toBeNull();
});

test("useCasesOf reads the catalog field and tolerates an older payload", () => {
  expect(useCasesOf(model("a", { useCases: ["coding"] }))).toEqual(["coding"]);
  expect(useCasesOf(model("a"))).toEqual([]);
  // An id this client does not know is dropped rather than rendered.
  expect(useCasesOf(model("a", { useCases: ["coding", "vision"] }))).toEqual(["coding"]);
});

test("modelThinks reads the thinks tag", () => {
  expect(modelThinks(model("a", { tags: ["thinks"] }))).toBe(true);
  expect(modelThinks(model("a", { tags: ["tool-use"] }))).toBe(false);
  expect(modelThinks(model("a"))).toBe(false);
});

test("pickForUseCase prefers the server's star, then a model of that use case", () => {
  const models = [
    model("w", { useCases: ["writing"] }),
    model("c", { useCases: ["coding"], useCasePicks: ["coding"] }),
    model("c2", { useCases: ["coding"] }),
  ];
  expect(pickForUseCase(models, "coding")?.id).toBe("c");
  // No star for it: the first model that belongs, never a stranger.
  expect(pickForUseCase(models, "reasoning")).toBeNull();
  expect(pickForUseCase([models[0], models[2]], "coding")?.id).toBe("c2");
});

test("alternativeThatSees names the closest curated model that accepts images", () => {
  const current = model("text-only", { size_gb: 5 });
  const small = model("small-vision", { acceptsImage: true, size_gb: 3 });
  const near = model("near-vision", { acceptsImage: true, size_gb: 6 });
  const far = model("far-vision", { acceptsImage: true, size_gb: 20 });
  const hub = model("hub-vision", { acceptsImage: true, size_gb: 5.5, source: "cached" });
  expect(alternativeThatSees([current, far, near, small, hub], current)?.id).toBe("near-vision");
  // Prefer one that fits this machine over a nearer one that does not.
  const tooBig = model("too-big", {
    acceptsImage: true,
    size_gb: 5.2,
    fit: { verdict: "no" } as AiCatalogModel["fit"],
  });
  expect(alternativeThatSees([current, tooBig, far], current)?.id).toBe("far-vision");
  expect(alternativeThatSees([current], current)).toBeNull();
});
