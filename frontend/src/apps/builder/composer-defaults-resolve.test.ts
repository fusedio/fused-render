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

const { CLAUDE_FALLBACK, CODEX_FALLBACK, COPILOT_FALLBACK, resetAgentBackendsForTests } =
  await import("@platform/lib/agent-backends");

test("another agent's pair is its default model and THAT model's effort", () => {
  // The Copilot row once the server knows the account's models: `auto`
  // takes no effort, each GPT id names its levels, one with its own default.
  const plan = {
    ...COPILOT_FALLBACK,
    enabled: true,
    models: [
      { id: "auto", label: "Auto (Copilot picks)", reasoning: false, efforts: [] },
      { id: "gpt-5.4", label: "GPT-5.4", efforts: ["low", "medium", "high", "xhigh"], default_effort: "high" },
      { id: "gpt-5.4-mini", label: "GPT-5.4 mini", efforts: ["low", "medium", "high"] },
    ],
  };
  resetAgentBackendsForTests({ backends: [CLAUDE_FALLBACK, plan], default: "claude", all: [CLAUDE_FALLBACK, plan, CODEX_FALLBACK] });
  try {
    // The default model takes no effort: the pair carries none, and the
    // composer draws no effort pick.
    expect(resolveComposerDefaults(null, "copilot")).toEqual({ model: "auto", effort: "" });
    expect(resolveComposerDefaults(null, "copilot", "gpt-5.4")).toEqual({ model: "gpt-5.4", effort: "high" });
    expect(resolveComposerDefaults(null, "copilot", "gpt-5.4-mini")).toEqual({ model: "gpt-5.4-mini", effort: "medium" });
    // A model the agent does not list falls to its default, effort and all.
    expect(resolveComposerDefaults(null, "copilot", "gpt-9")).toEqual({ model: "auto", effort: "" });
    // The global pair is Claude's and never applies to another agent.
    expect(resolveComposerDefaults({ model: "opus", effort: "max" }, "copilot", "gpt-5.4"))
      .toEqual({ model: "gpt-5.4", effort: "high" });
  } finally {
    resetAgentBackendsForTests();
  }
});

test("the static Copilot row already carries no effort for auto", () => {
  resetAgentBackendsForTests();
  expect(resolveComposerDefaults(null, "copilot")).toEqual({ model: "auto", effort: "" });
  expect(resolveComposerDefaults(null, "copilot", "gpt-5.4")).toEqual({ model: "gpt-5.4", effort: DEFAULT_EFFORT });
});
