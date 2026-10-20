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
  expect("refuse" in rc("copilot", true)).toBe(true);
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
});
