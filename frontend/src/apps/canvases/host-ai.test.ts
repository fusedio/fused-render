import { expect, test } from "bun:test";

import { CLAUDE_FALLBACK, type AgentCatalog } from "@platform/lib/agent-backends";
import { hostAiModels } from "./host-ai";

const catalog = (over: Partial<AgentCatalog>): AgentCatalog =>
  ({ default: "claude", backends: [CLAUDE_FALLBACK], all: [CLAUDE_FALLBACK], ...over }) as AgentCatalog;

test("a frame starts on the default agent's default model", () => {
  expect(hostAiModels(catalog({})).default).toBe("claude:sonnet");
});

test("with the default agent missing, it falls back to the first agent's own default, not its lightest row", () => {
  // The lists read Haiku first now; "first row" would start every such frame on Haiku.
  expect(hostAiModels(catalog({ default: "copilot" })).default).toBe("claude:sonnet");
});
