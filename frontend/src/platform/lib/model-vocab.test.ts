import { expect, test } from "bun:test";

import { normalizeModel } from "./model-vocab";

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
