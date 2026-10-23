import { describe, expect, test } from "bun:test";

import { classifyCommand } from "../protocol/commands";
import { localEffortArg, localModelArg, planLocalCommand } from "./local-commands";

const plan = (text: string, worktreeReady = true) =>
  planLocalCommand(classifyCommand(text)!, { worktreeReady });

describe("planLocalCommand", () => {
  test("/clear, /help and /tasks refuse words after them instead of dropping them", () => {
    expect(plan("/clear")).toEqual({ do: "clear" });
    expect(plan("/tasks")).toEqual({ do: "tasks" });
    for (const t of ["/clear\nplease fix the bug", "/tasks now", "/help me"]) {
      expect("refuse" in plan(t)).toBe(true);
    }
  });

  test("/worktree is refused when no menu can act on it (peek, file, non-repo)", () => {
    const r = plan("/worktree feat", false);
    expect("refuse" in r && r.refuse).toContain("git repository");
    expect(plan("/worktree feat")).toEqual({ do: "worktree", name: "feat" });
    expect(plan("/worktree")).toEqual({ do: "worktree" });
    const bad = plan("/worktree a b");
    expect("refuse" in bad && bad.refuse).toContain("[name]");
  });

  test("a bad /model or /effort says what is allowed", () => {
    const m = plan("/model gpt-4");
    expect("refuse" in m && m.refuse).toContain("<name>");
    expect("refuse" in m && m.refuse).toContain("opus");
    const e = plan("/effort extreme");
    expect("refuse" in e && e.refuse).toContain("<low|medium|high|max>");
    expect(plan("/effort HIGH")).toEqual({ do: "effort", value: "high" });
  });
});

test("/remote-control needs Claude and a session", () => {
  const rc = (backend: string, hasSession: boolean) =>
    planLocalCommand(classifyCommand("/remote-control  my laptop ")!, { worktreeReady: true, backend, hasSession });
  expect(rc("claude", true)).toEqual({ do: "remote-control", name: "my laptop" });
  expect(rc("", true)).toEqual({ do: "remote-control", name: "my laptop" });
  expect("refuse" in rc("copilot", true)).toBe(true);
  const codex = rc("codex", true);
  expect("refuse" in codex && codex.refuse).toContain("not available for Codex");
  const none = rc("claude", false);
  expect("refuse" in none && none.refuse).toContain("first message");
});

describe("/model argument", () => {
  test("shortlist id or label, or a full claude id passed through", () => {
    expect(localModelArg("Opus")).toBe("opus");
    expect(localModelArg("claude-opus-4-1-20250805")).toBe("claude-opus-4-1-20250805");
    expect(localModelArg("claude-3.5-sonnet")).toBe("claude-3.5-sonnet");
    expect(localModelArg("gpt-4")).toBe("");
    expect(localModelArg("")).toBe("");
    expect(localEffortArg("max")).toBe("max");
    expect(localEffortArg("extreme")).toBe("");
  });

  test("Codex's shortlist and its own full ids, never Claude's", () => {
    // The static CODEX_FALLBACK row answers before the catalog lands.
    expect(localModelArg("gpt-6-luna", "codex")).toBe("gpt-6-luna");
    expect(localModelArg("GPT-6.1 Sol", "codex")).toBe("gpt-6.1-sol");
    expect(localModelArg("gpt-5.6-terra", "codex")).toBe("gpt-5.6-terra");
    // A newer or plan-only id is passed through as typed (§8: a seed).
    expect(localModelArg("gpt-7-nova", "codex")).toBe("gpt-7-nova");
    expect(localModelArg("gpt 6", "codex")).toBe("");
    expect(localModelArg("gpt-6-luna")).toBe("");
    expect(localEffortArg("xhigh", "codex")).toBe("xhigh");
    const bad = planLocalCommand(classifyCommand("/model ???")!, { worktreeReady: true, backend: "codex" });
    expect("refuse" in bad && bad.refuse).toContain("gpt-6-luna, gpt-6.1-sol, gpt-5.6-terra, gpt-5.6-luna, or a full model id");
  });

  test("Codex has no worktrees and no background tasks (§7)", () => {
    const at = (text: string) => planLocalCommand(classifyCommand(text)!, { worktreeReady: true, backend: "codex" });
    const wt = at("/worktree feat");
    expect("refuse" in wt && wt.refuse).toBe("Codex has no worktrees");
    const tasks = at("/tasks");
    expect("refuse" in tasks && tasks.refuse).toBe("Codex has no background tasks");
  });
});

describe("/effort per model", () => {
  test("validates against the levels the chat's model takes, and says so for a model that takes none", async () => {
    const { CLAUDE_FALLBACK, CODEX_FALLBACK, COPILOT_FALLBACK, resetAgentBackendsForTests } =
      await import("@platform/lib/agent-backends");
    const plan = {
      ...COPILOT_FALLBACK,
      enabled: true,
      models: [
        { id: "auto", label: "Auto (Copilot picks)", reasoning: false, efforts: [] },
        { id: "gpt-5.4", label: "GPT-5.4", efforts: ["low", "medium", "high", "xhigh"] },
      ],
    };
    resetAgentBackendsForTests({ backends: [CLAUDE_FALLBACK, plan], default: "claude", all: [CLAUDE_FALLBACK, plan, CODEX_FALLBACK] });
    try {
      expect(localEffortArg("xhigh", "copilot", "gpt-5.4")).toBe("xhigh");
      expect(localEffortArg("max", "copilot", "gpt-5.4")).toBe("");
      expect(localEffortArg("high", "copilot", "auto")).toBe("");
      // No model named: the agent's whole list, as before.
      expect(localEffortArg("max", "copilot")).toBe("max");
      const at = (text: string, model: string) =>
        planLocalCommand(classifyCommand(text)!, { worktreeReady: true, backend: "copilot", model });
      const none = at("/effort high", "auto");
      expect("refuse" in none && none.refuse).toBe("auto takes no effort level");
      const bad = at("/effort max", "gpt-5.4");
      expect("refuse" in bad && bad.refuse).toContain("one of low, medium, high, xhigh");
      expect(at("/effort xhigh", "gpt-5.4")).toEqual({ do: "effort", value: "xhigh" });
    } finally {
      resetAgentBackendsForTests();
    }
  });
});
