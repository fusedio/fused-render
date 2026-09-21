import { afterEach, beforeEach, describe, expect, test } from "bun:test";

// This module transitively imports "@platform/lib/router", whose module-init
// reads `location` at import time (router.ts:54) — so these globals must be
// in place BEFORE that import executes. A static `import` is hoisted ahead
// of any top-level statement in this file regardless of where it's written,
// so (mirroring RepoUpdatesDock.test.tsx's own documented fix for the same
// problem) the globals are set here first and the module under test is
// loaded with a dynamic `await import` afterward, which runs in the order
// written rather than being hoisted.
(globalThis as Record<string, unknown>).location = { pathname: "/x", search: "" };
(globalThis as Record<string, unknown>).window = {
  parent: undefined,
  top: undefined,
  dispatchEvent: () => true,
};
(globalThis as Record<string, unknown>).history = {
  state: null,
  replaceState: () => {},
  pushState: () => {},
};

const { explainErrorPrompt, explainWithAi, resetDefaultFolderCache, resolveDefaultFolder } =
  await import("@platform/lib/explain-with-ai");
const { peekPendingClaudeAsk, takePendingClaudeAsk } = await import(
  "@platform/lib/pending-claude-ask"
);

describe("explainErrorPrompt", () => {
  test("includes the raw message", () => {
    const prompt = explainErrorPrompt("could not reach the server");
    expect(prompt).toContain("could not reach the server");
  });

  test("never instructs Claude to actually fix or change anything", () => {
    const prompt = explainErrorPrompt("could not reach the server").toLowerCase();
    // The only mention of "fix" allowed is the explicit prohibition tested
    // below ("do not fix anything") — an affirmative instruction like
    // repoFixPrompt's "...then fix it" or "run whatever is needed" must
    // never appear here.
    expect(prompt).not.toContain("run whatever is needed");
    expect(prompt).not.toContain("then fix it");
  });

  test("tells Claude to stop after explaining", () => {
    const prompt = explainErrorPrompt("boom");
    expect(prompt.toLowerCase()).toContain("do not fix anything");
  });

  test("appends optional context after the message, not in place of it", () => {
    const prompt = explainErrorPrompt("boom", "This happened while deploying.");
    expect(prompt).toContain("boom");
    expect(prompt).toContain("This happened while deploying.");
    expect(prompt.indexOf("boom")).toBeLessThan(prompt.indexOf("This happened while deploying."));
  });

  test("omits the context paragraph entirely when none is given", () => {
    const prompt = explainErrorPrompt("boom");
    // No stray blank paragraph / undefined text from a skipped context.
    expect(prompt).not.toContain("undefined");
  });
});

// `getConfig()` is a thin `getJson("/api/config")` wrapper — this file
// follows FilesHome.render.test.tsx's own documented convention of stubbing
// `globalThis.fetch` (a plain, unfrozen global) rather than `mock.module`ing
// "@platform/lib/api" itself, which RepoUpdatesDock.test.tsx's header found
// breaks OTHER test files sharing the same process.
describe("resolveDefaultFolder", () => {
  const realFetch = globalThis.fetch;

  beforeEach(() => {
    resetDefaultFolderCache();
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
    resetDefaultFolderCache();
  });

  test("resolves Config.fused_dir from /api/config", async () => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ fused_dir: "/Users/me/Fused" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;
    expect(await resolveDefaultFolder()).toBe("/Users/me/Fused");
  });

  test("a second call reuses the cached value — no second fetch", async () => {
    let calls = 0;
    globalThis.fetch = (async () => {
      calls += 1;
      return new Response(JSON.stringify({ fused_dir: "/Users/me/Fused" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }) as unknown as typeof fetch;
    await resolveDefaultFolder();
    await resolveDefaultFolder();
    expect(calls).toBe(1);
  });

  test("a failed fetch resolves to undefined and does not poison the cache for later calls", async () => {
    globalThis.fetch = (async () => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    expect(await resolveDefaultFolder()).toBeUndefined();

    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ fused_dir: "/Users/me/Fused" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;
    expect(await resolveDefaultFolder()).toBe("/Users/me/Fused");
  });

  test("B5 (FIXES-round-1.md): an empty fused_dir does not permanently strand later calls", async () => {
    // A SUCCESSFUL config fetch that resolves an empty `fused_dir` — this is
    // NOT the `.catch` path, so before the fix `inFlight` was never cleared
    // and `resolveDefaultFolder` (seeing `cachedDefaultFolder === undefined`,
    // since an empty string was never assigned here) would keep handing back
    // the SAME stale resolved promise on every later call, forever, even
    // once a real value became available.
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({}), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;
    expect(await resolveDefaultFolder()).toBeUndefined();

    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ fused_dir: "/Users/me/Fused" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;
    expect(await resolveDefaultFolder()).toBe("/Users/me/Fused");
  });
});

describe("explainWithAi", () => {
  const realFetch = globalThis.fetch;

  beforeEach(() => {
    resetDefaultFolderCache();
    // `takePendingClaudeAsk` only clears a SPECIFIC path (by design — see
    // pending-claude-ask.ts), so a fixed "/anything" guess (as
    // pending-claude-ask.test.ts itself uses) would miss a stale entry left
    // by an earlier test in THIS describe that staged a real path. Peek the
    // actual pending path, if any, and clear that one instead.
    const stale = peekPendingClaudeAsk();
    if (stale) takePendingClaudeAsk(stale.path);
  });

  afterEach(() => {
    globalThis.fetch = realFetch;
    resetDefaultFolderCache();
  });

  test("a folder-scoped call stages the ask against that folder without touching config", async () => {
    globalThis.fetch = (async () => {
      throw new Error("must not be called — a folderPath was given");
    }) as unknown as typeof fetch;
    await explainWithAi("explain this", "/Users/me/repo");
    expect(peekPendingClaudeAsk()).toEqual({ path: "/Users/me/repo", prompt: "explain this" });
  });

  test("a folderless call resolves the default folder and stages against it", async () => {
    globalThis.fetch = (async () =>
      new Response(JSON.stringify({ fused_dir: "/Users/me/Fused" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })) as unknown as typeof fetch;
    await explainWithAi("explain this");
    expect(peekPendingClaudeAsk()).toEqual({ path: "/Users/me/Fused", prompt: "explain this" });
  });

  test("no folder available (config fetch fails) is a silent no-op — nothing staged", async () => {
    globalThis.fetch = (async () => {
      throw new Error("network down");
    }) as unknown as typeof fetch;
    await explainWithAi("explain this");
    expect(peekPendingClaudeAsk()).toBeNull();
  });
});
