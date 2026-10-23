// The per-agent setup doors (api.ts `agentRoute`): each agent's health,
// install, sign-in and PATH calls reach that agent's own `/api/<id>/*`, and an
// id the app does not know falls back to Claude's, never to a made-up route.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();
import { afterEach, beforeEach, expect, test } from "bun:test";

import {
  cancelAgentLogin,
  getAgentHealth,
  getAgentInstall,
  getAgentLogin,
  linkAgentPath,
  linkClaudePath,
  refreshAgentHealth,
  startAgentInstall,
  startAgentLogin,
} from "./api";

let urls: string[];
let realFetch: typeof fetch;

beforeEach(() => {
  urls = [];
  realFetch = globalThis.fetch;
  globalThis.fetch = (async (url: string) => {
    urls.push(String(url));
    return { ok: true, status: 200, json: async () => ({ ok: true }) } as Response;
  }) as unknown as typeof fetch;
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

const path = (u: string) => new URL(u, "http://x").pathname;

test("gemini's doors are /api/gemini/* (docs/GEMINI_BACKEND.md D10)", async () => {
  await getAgentHealth("gemini");
  await refreshAgentHealth("gemini");
  await startAgentLogin("gemini");
  await linkAgentPath("gemini");
  expect(urls.map(path)).toEqual([
    "/api/gemini/health",
    "/api/gemini/health/refresh",
    "/api/gemini/login",
    "/api/gemini/link-path",
  ]);
});

test("codex's doors are /api/codex/* (docs/CODEX_BACKEND.md D10)", async () => {
  await getAgentHealth("codex");
  await refreshAgentHealth("codex");
  await startAgentInstall("codex");
  await getAgentInstall("codex");
  await startAgentLogin("codex");
  await getAgentLogin("codex");
  await cancelAgentLogin("codex");
  await linkAgentPath("codex");
  expect(urls.map(path)).toEqual([
    "/api/codex/health",
    "/api/codex/health/refresh",
    "/api/codex/install",
    "/api/codex/install",
    "/api/codex/login",
    "/api/codex/login",
    "/api/codex/login/cancel",
    "/api/codex/link-path",
  ]);
});

test("every known agent keeps its own prefix; anything else is Claude's", async () => {
  await getAgentHealth("claude");
  await getAgentHealth("copilot");
  await getAgentHealth("");
  await getAgentHealth("../etc");
  await linkClaudePath();
  expect(urls.map(path)).toEqual([
    "/api/claude/health",
    "/api/copilot/health",
    "/api/claude/health",
    "/api/claude/health",
    "/api/claude/link-path",
  ]);
});
