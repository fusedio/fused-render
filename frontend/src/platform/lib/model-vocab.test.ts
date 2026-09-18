import { expect, test } from "bun:test";

import { listedModelIn, normalizeModel } from "./model-vocab";

test("every Fable spelling is said as fable, and nothing else is touched", () => {
  // The pinned id the pickers used to offer, a dated full id, the bare alias.
  expect(normalizeModel("claude-fable-5-1")).toBe("fable");
  expect(normalizeModel("claude-fable-5-1-20260401")).toBe("fable");
  expect(normalizeModel("claude-fable-5")).toBe("fable");
  expect(normalizeModel("Claude-Fable-5-1")).toBe("fable");
  expect(normalizeModel("fable")).toBe("fable");
  // Other models, unknown ids and "nothing chosen" pass through as they are.
  expect(normalizeModel("opus")).toBe("opus");
  expect(normalizeModel("claude-opus-4-6")).toBe("claude-opus-4-6");
  expect(normalizeModel("fabled-thing")).toBe("fabled-thing");
  expect(normalizeModel("")).toBe("");
  expect(normalizeModel(undefined)).toBe("");
  expect(normalizeModel("  haiku ")).toBe("haiku");
});

test("the CLI's [1m] context qualifier rides along — it is a modifier, not a model", () => {
  expect(normalizeModel("claude-fable-5-1[1m]")).toBe("fable[1m]");
  expect(normalizeModel("fable[1m]")).toBe("fable[1m]");
  expect(normalizeModel("opus[1m]")).toBe("opus[1m]");
});

test("listedModelIn finds the row a stored value means, qualifier or not, and nothing else", () => {
  const MODELS = ["fable", "opus", "sonnet", "haiku"];
  expect(listedModelIn("fable", MODELS)).toBe("fable");
  expect(listedModelIn("claude-fable-5-1", MODELS)).toBe("fable");
  // The qualifier is not on this list; the model under it is.
  expect(listedModelIn("claude-fable-5-1[1m]", MODELS)).toBe("fable");
  expect(listedModelIn("opus[1m]", MODELS)).toBe("opus");
  // A list that offers the qualified row keeps it.
  expect(listedModelIn("opus[1m]", [...MODELS, "opus[1m]"])).toBe("opus[1m]");
  // Not on the list at all.
  expect(listedModelIn("claude-opus-9", MODELS)).toBe("");
  expect(listedModelIn("", MODELS)).toBe("");
  expect(listedModelIn(undefined, MODELS)).toBe("");
});
