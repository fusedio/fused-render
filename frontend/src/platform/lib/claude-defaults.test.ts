// The global model / thinking store: read-only (the Claude settings page is
// the one editor of `~/.claude/settings.json`), and a settings-page save
// reaches every open surface through `refreshClaudeDefaults`.
import { installDomShim } from "@platform/lib/testDomShim";
installDomShim();

import { afterEach, beforeEach, expect, test } from "bun:test";

const realFetch = globalThis.fetch;
type Pair = { model: string; effort: string };

/** A fetch stub whose answers are PROMISES THE TEST RESOLVES, so the order the
 *  network answers in is the order the test chooses. */
function stub() {
  const pending: { url: string; method: string; body: unknown; resolve: (r: Response) => void }[] = [];
  globalThis.fetch = ((url: string, init?: RequestInit) =>
    new Promise<Response>((resolve) => {
      pending.push({
        url: String(url),
        method: init?.method ?? "GET",
        body: init?.body ? JSON.parse(String(init.body)) : null,
        resolve,
      });
    })) as unknown as typeof fetch;
  const answer = (i: number, pair: Pair, status = 200) =>
    pending[i].resolve(new Response(JSON.stringify(pair), {
      status, headers: { "Content-Type": "application/json" },
    }));
  return { pending, answer };
}

let mod: typeof import("./claude-defaults");
beforeEach(async () => {
  mod = await import("./claude-defaults");
  mod.resetClaudeDefaultsForTests();
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

test("the store has no writer: only the settings page edits the global pair", () => {
  // Akshil, 2026-10-28: a pick on the New app composer used to PUT here and
  // move every later Claude run on the machine.
  expect("setClaudeDefaults" in mod).toBe(false);
});

test("a refresh re-reads the file and tells every subscriber and tab", async () => {
  const s = stub();
  const stored = new Map<string, string>();
  const g = globalThis as { localStorage?: unknown };
  const realStorage = g.localStorage;
  g.localStorage = { setItem: (k: string, v: string) => stored.set(k, v) };
  const heard: Pair[] = [];
  mod.subscribeClaudeDefaults((d) => heard.push(d));
  const done = mod.refreshClaudeDefaults();
  expect(s.pending[0].method).toBe("GET");
  s.answer(0, { model: "sonnet", effort: "high" });
  expect(await done).toEqual({ model: "sonnet", effort: "high" });
  expect(heard).toEqual([{ model: "sonnet", effort: "high" }]);
  g.localStorage = realStorage;
  const sent = JSON.parse(stored.get(mod.CLAUDE_DEFAULTS_BROADCAST_KEY) ?? "{}");
  expect([sent.model, sent.effort]).toEqual(["sonnet", "high"]);
});

test("a refresh does not share a read that departed before the save", async () => {
  const s = stub();
  const stale = mod.readClaudeDefaults();   // GET #0, the file before the save
  const fresh = mod.refreshClaudeDefaults(); // GET #1, its own request
  expect(s.pending.length).toBe(2);
  s.answer(0, { model: "fable", effort: "low" });
  await stale;
  s.answer(1, { model: "opus", effort: "max" });
  await fresh;
  expect(mod.getClaudeDefaults()).toEqual({ model: "opus", effort: "max" });
});
