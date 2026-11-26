// The Agent pill's choices (AgentSelect `only`): an expert's chat runs on Claude or Copilot alone.
import { describe, expect, test } from "bun:test";
import { fixedAgentTitle, offeredAgents } from "./AgentSelect";

describe("offeredAgents", () => {
  test("every enabled agent when the chat names none", () => {
    expect(offeredAgents(["claude", "copilot", "codex"])).toEqual(["claude", "copilot", "codex"]);
  });
  test("only the chat's own, in the catalog's order, and only those switched on", () => {
    expect(offeredAgents(["codex", "copilot", "claude"], ["claude", "copilot"])).toEqual(["copilot", "claude"]);
    expect(offeredAgents(["claude", "codex"], ["claude", "copilot"])).toEqual(["claude"]);
  });
  test("the fixed pill's tooltip names only the agents a new chat here could use", () => {
    const title = fixedAgentTitle("copilot", offeredAgents(["claude", "copilot", "codex"], ["claude", "copilot"]));
    expect(title).toContain("Start a new chat to use Claude.");
    expect(title).not.toContain("Codex");
  });
});
