import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { expect, test } from "bun:test";

const { resolveComposerDefaults } = await import("./composer-defaults-resolve");
const { DEFAULT_EFFORT, DEFAULT_MODEL } = await import("@apps/claude/ui/composer-defaults");

test("unknown (null) and empty defaults both resolve to the fallback pair", () => {
  const fallback: object = { model: DEFAULT_MODEL, effort: DEFAULT_EFFORT };
  expect<object>(resolveComposerDefaults(null)).toEqual(fallback);
  expect<object>(resolveComposerDefaults({ model: "", effort: "" })).toEqual(fallback);
});

test("the global pair wins when this build offers it", () => {
  expect(resolveComposerDefaults({ model: "opus", effort: "xhigh" }))
    .toEqual({ model: "opus", effort: "xhigh" });
});

test("each half falls back on its own; never an empty value", () => {
  expect(resolveComposerDefaults({ model: "gpt-4", effort: "max" }))
    .toEqual({ model: DEFAULT_MODEL, effort: "max" });
  expect(resolveComposerDefaults({ model: "haiku", effort: "ultra" }))
    .toEqual({ model: "haiku", effort: DEFAULT_EFFORT });
});

test("a legacy full Fable id folds onto the alias", () => {
  expect(resolveComposerDefaults({ model: "claude-fable-5-1", effort: "low" }).model)
    .toBe("fable");
});
