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
