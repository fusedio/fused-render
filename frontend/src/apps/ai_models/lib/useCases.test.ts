import { expect, test } from "bun:test";

import type { AiCatalogModel } from "@platform/lib/api";
import { USE_CASES, groupByUseCase, useCaseOf } from "./useCases";

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

test("three use cases, in reading order, each with a label", () => {
  expect(USE_CASES.map((u) => u.id)).toEqual(["writing", "coding", "reasoning"]);
  expect(USE_CASES.map((u) => u.label)).toEqual(["Writing & chat", "Coding", "Deep reasoning"]);
});

test("useCaseOf takes the first known use case and defaults to writing", () => {
  expect(useCaseOf(model("a", { useCases: ["coding"] }))).toBe("coding");
  expect(useCaseOf(model("a", { useCases: ["vision", "reasoning"] }))).toBe("reasoning");
  expect(useCaseOf(model("a", { useCases: [] }))).toBe("writing");
  expect(useCaseOf(model("a"))).toBe("writing");
});

test("groupByUseCase: USE_CASES order, rows keep their order, empty groups dropped", () => {
  const models = [
    model("note", { useCases: ["writing"] }),
    model("coder-b", { useCases: ["coding"] }),
    model("big", { useCases: ["reasoning", "writing"] }),
    model("coder-a", { useCases: ["coding"] }),
    model("untagged"),
  ];
  const groups = groupByUseCase(models, useCaseOf);
  expect(groups.map((g) => g.useCase.id)).toEqual(["writing", "coding", "reasoning"]);
  expect(groups[0].items.map((m) => m.id)).toEqual(["note", "untagged"]);
  expect(groups[1].items.map((m) => m.id)).toEqual(["coder-b", "coder-a"]);
  expect(groups[2].items.map((m) => m.id)).toEqual(["big"]);
  const only = groupByUseCase([model("x", { useCases: ["coding"] })], useCaseOf);
  expect(only.map((g) => g.useCase.id)).toEqual(["coding"]);
});
