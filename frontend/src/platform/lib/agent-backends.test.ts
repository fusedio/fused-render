// The catalog's static rows, before the server answers. The field-for-field
// match against the server's registry is tests/test_agent_backend_pins.py;
// this pins what the FIRST FRAME does with them.
import { afterEach, expect, test } from "bun:test";

import {
  CLAUDE_FALLBACK,
  CODEX_FALLBACK,
  COPILOT_FALLBACK,
  agentLabel,
  agentNamed,
  backendOf,
  capsOf,
  defaultBackend,
  defaultModelOf,
  isBackendEnabled,
  isForeignModel,
  modesOf,
  resetAgentBackendsForTests,
} from "./agent-backends";

afterEach(() => resetAgentBackendsForTests());

test("a Codex chat answers with Codex's row before the catalog lands (docs/CODEX_BACKEND.md §7)", () => {
  resetAgentBackendsForTests();
  expect(backendOf("codex")).toBe(CODEX_FALLBACK);
  expect(agentLabel("codex")).toBe("Codex");
  expect(defaultModelOf("codex")).toBe("gpt-6-luna");
  // No plan mode; both live switches on — model and mode are per turn.
  expect(modesOf("codex")).toEqual(["prompt", "acceptEdits", "auto"]);
  const caps = capsOf("codex");
  expect(caps.plan_mode).toBe(false);
  expect(caps.live_model_switch).toBe(true);
  expect(caps.live_mode_switch).toBe(true);
  expect(caps.snapshots || caps.artifacts || caps.worktrees || caps.bg_tasks).toBe(false);
  expect(caps.ask_user || caps.terminal_mirror || caps.mid_turn_messages).toBe(false);
});

test("Codex is off until the server says otherwise (D17)", () => {
  resetAgentBackendsForTests();
  expect(CODEX_FALLBACK.enabled).toBe(false);
  expect(isBackendEnabled("codex")).toBe(false);
  expect(defaultBackend()).toBe("claude");
  // A Codex task stays named even with Claude alone on.
  expect(agentNamed("codex")).toBe(true);
});

test("a Codex model on a Claude chat is foreign, and the other way round", () => {
  resetAgentBackendsForTests();
  expect(isForeignModel("gpt-6-luna", "claude")).toBe(true);
  expect(isForeignModel("sonnet", "codex")).toBe(true);
  expect(isForeignModel("gpt-6-luna", "codex")).toBe(false);
  // An id no agent lists is a pinned or newer one, carried as typed.
  expect(isForeignModel("gpt-7-nova", "codex")).toBe(false);
});

test("with all three on, the server's default can be Codex", () => {
  const rows = [CLAUDE_FALLBACK, COPILOT_FALLBACK, CODEX_FALLBACK].map((r) => ({ ...r, enabled: true }));
  resetAgentBackendsForTests({ backends: rows, default: "codex", all: rows });
  expect(defaultBackend()).toBe("codex");
  expect(isBackendEnabled("codex")).toBe(true);
});

// ---- per-model efforts (Copilot's `models.list`, carried in its catalog row)

import {
  COPILOT_FALLBACK as COPILOT_STATIC,
  defaultEffortFor,
  effortsForModel,
  effortsOf,
  isUnlistedEffort,
  modelOf,
} from "./agent-backends";

/** The Copilot row as the server serves it once the account's model list
 *  is known: `auto` and the Claude id take no effort, each GPT id names its
 *  levels (one with the CLI's own default), and a row from an older server
 *  carries no `efforts` key at all. */
const COPILOT_PLAN = {
  ...COPILOT_STATIC,
  enabled: true,
  models: [
    { id: "auto", label: "Auto (Copilot picks)", reasoning: false, efforts: [] },
    { id: "gpt-5.4", label: "GPT-5.4", reasoning: true, efforts: ["low", "medium", "high", "xhigh"], default_effort: "high" },
    { id: "gpt-5.4-mini", label: "GPT-5.4 mini", reasoning: true, efforts: ["low", "medium", "high"] },
    { id: "claude-sonnet-4.5", label: "Claude Sonnet 4.5", reasoning: false, efforts: [] },
    { id: "gemini-3-pro", label: "Gemini 3 Pro" },
  ],
};

function seedPlan() {
  resetAgentBackendsForTests({
    backends: [CLAUDE_FALLBACK, COPILOT_PLAN],
    default: "claude",
    all: [CLAUDE_FALLBACK, COPILOT_PLAN, CODEX_FALLBACK],
  });
}

test("the efforts are the MODEL's where the catalog knows them per model", () => {
  seedPlan();
  expect(effortsForModel("copilot", "gpt-5.4")).toEqual(["low", "medium", "high", "xhigh"]);
  expect(effortsForModel("copilot", "gpt-5.4-mini")).toEqual(["low", "medium", "high"]);
  // None at all: the pill is hidden on this.
  expect(effortsForModel("copilot", "auto")).toEqual([]);
  expect(effortsForModel("copilot", "claude-sonnet-4.5")).toEqual([]);
  // No per-model list (an older server): the agent's whole list.
  expect(effortsForModel("copilot", "gemini-3-pro")).toEqual(effortsOf("copilot"));
  // A model the agent does not list (pinned or newer): the agent's list, as
  // the spawn passes an uncataloged id through with its effort.
  expect(effortsForModel("copilot", "gpt-9")).toEqual(effortsOf("copilot"));
  expect(effortsForModel("copilot", "")).toEqual(effortsOf("copilot"));
  expect(effortsForModel("claude", "opus")).toEqual(effortsOf("claude"));
  expect(modelOf("copilot", "gpt-5.4")?.default_effort).toBe("high");
  expect(modelOf("copilot", "nope")).toBeUndefined();
});

test("a model's default effort is the CLI's own, else medium, else its first, else none", () => {
  seedPlan();
  expect(defaultEffortFor("copilot", "gpt-5.4")).toBe("high");
  expect(defaultEffortFor("copilot", "gpt-5.4-mini")).toBe("medium");
  expect(defaultEffortFor("copilot", "auto")).toBe("");
  expect(defaultEffortFor("copilot", "claude-sonnet-4.5")).toBe("");
  expect(defaultEffortFor("claude", "opus")).toBe("medium");
  const lowOnly = {
    ...COPILOT_PLAN,
    models: [{ id: "tiny", label: "Tiny", efforts: ["low"], default_effort: "max" }],
  };
  resetAgentBackendsForTests({ backends: [CLAUDE_FALLBACK, lowOnly], default: "claude", all: [CLAUDE_FALLBACK, lowOnly, CODEX_FALLBACK] });
  // A default the list does not hold is no default.
  expect(defaultEffortFor("copilot", "tiny")).toBe("low");
});

test("an effort is unlisted per model when a model is named, per agent otherwise", () => {
  seedPlan();
  expect(isUnlistedEffort("max", "copilot", "gpt-5.4")).toBe(true);
  expect(isUnlistedEffort("xhigh", "copilot", "gpt-5.4")).toBe(false);
  expect(isUnlistedEffort("max", "copilot")).toBe(false);
  expect(isUnlistedEffort("high", "copilot", "auto")).toBe(true);
  expect(isUnlistedEffort("", "copilot", "auto")).toBe(false);
  expect(isUnlistedEffort("high", "copilot", "gpt-9")).toBe(false);
});

test("before the catalog lands, the static row already hides the pill for auto and the Claude ids", () => {
  resetAgentBackendsForTests();
  expect(effortsForModel("copilot", "auto")).toEqual([]);
  expect(effortsForModel("copilot", "claude-opus-4.5")).toEqual([]);
  expect(effortsForModel("copilot", "gpt-5.4")).toEqual(COPILOT_FALLBACK.efforts);
  expect(defaultEffortFor("copilot", "auto")).toBe("");
});
